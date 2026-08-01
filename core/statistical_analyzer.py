"""Statistical analysis of A/B model comparison outcomes.

This module turns per-sample comparison scores into the statistics a model
validator needs to defend a conclusion to a regulator: a win rate with a proper
confidence interval, a significance test, an effect size, multiple-comparison
control, and the power / minimum-detectable-effect context that tells you whether
the sample was large enough to trust the result.

Design notes
------------
* Inputs are paired: ``scores_a[i]`` and ``scores_b[i]`` are the quality scores of
  Branch A (baseline) and Branch B (challenger) on the *same* evaluation sample
  ``i`` (higher = better). This mirrors how an evaluation set is run through two
  model branches, and is why the default significance test here is a *paired*
  t-test with a paired effect size (Cohen's d_z) — see :func:`analyze`.
* Where the scores come from is the caller's responsibility. ForkMark does not
  define "quality" for a use case: the per-sample score may come from an
  auto-evaluator (e.g. an LLM-as-judge), a human rating, or a task metric. For
  regulated use that scoring method is itself part of the model and should be
  documented and validated — otherwise the statistics are only as trustworthy as
  the scores fed into them.
* Only :mod:`scipy` (and :mod:`numpy`) are used, to keep the dependency footprint
  minimal and the numerics auditable. Every formula carries a literature citation.
* All public functions are pure and deterministic, which makes them
  straightforward to unit-test and to reproduce for a validation memo.

References
----------
Wilson, E. B. (1927). Probable inference, the law of succession, and statistical
    inference. JASA 22(158), 209-212.
Brown, L. D., Cai, T. T., & DasGupta, A. (2001). Interval estimation for a
    binomial proportion. Statistical Science 16(2), 101-133.
Welch, B. L. (1947). The generalisation of "Student's" problem when several
    different population variances are involved. Biometrika 34(1/2), 28-35.
Cohen, J. (1988). Statistical Power Analysis for the Behavioral Sciences
    (2nd ed.). Lawrence Erlbaum Associates.
Benjamini, Y., & Hochberg, Y. (1995). Controlling the false discovery rate: a
    practical and powerful approach to multiple testing. JRSS-B 57(1), 289-300.
"""
from __future__ import annotations

import dataclasses
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Union

import numpy as np
import numpy.typing as npt
from scipy import stats  # type: ignore[import-untyped]

# A vector of floats, accepting either a plain sequence or a NumPy float array.
FloatVector = Union[Sequence[float], npt.NDArray[np.float64]]

__all__ = [
    "StatisticalResult",
    "wilson_score_interval",
    "welch_t_test",
    "paired_t_test",
    "cohens_d",
    "paired_cohens_d",
    "benjamini_hochberg",
    "minimum_detectable_effect",
    "power_analysis",
    "analyze",
    "analyze_batch",
]

logger = logging.getLogger("forkmark.statistics")

# Default significance level and reporting confidence.
_DEFAULT_ALPHA = 0.05
_DEFAULT_CONFIDENCE = 0.95
# Conventional target power for sample-size / MDE reporting (Cohen, 1988).
_DEFAULT_POWER = 0.80


@dataclass(frozen=True)
class StatisticalResult:
    """The statistical summary of a single A/B comparison.

    Attributes:
        win_rate:                 Fraction of samples where Branch A scores higher
                                  than Branch B (ties count as one half), in [0, 1].
        ci_lower:                 Lower bound of the 95% Wilson interval for win_rate.
        ci_upper:                 Upper bound of the 95% Wilson interval for win_rate.
        p_value:                  Raw two-sided Welch's t-test p-value.
        adjusted_p_value:         p-value after Benjamini-Hochberg FDR adjustment
                                  across a batch; equals ``p_value`` for a single test.
        effect_size:              Cohen's d (signed: positive favours Branch A).
        is_significant:           True iff ``adjusted_p_value`` < alpha (default 0.05).
        sample_size:              Number of paired evaluation samples, n.
        minimum_detectable_effect:Smallest effect (Cohen's d, matching ``method``)
                                  detectable at this sample size with power=0.80,
                                  alpha=0.05 (normal approximation).
        method:                   Significance/effect-size method used:
                                  "paired_t_test" (default; paired t-test with
                                  Cohen's d_z, appropriate because the two branches
                                  are scored on the same samples) or "welch_t_test"
                                  (independent-samples Welch t-test with pooled d).
    """

    win_rate: float
    ci_lower: float
    ci_upper: float
    p_value: float
    adjusted_p_value: float
    effect_size: float
    is_significant: bool
    sample_size: int
    minimum_detectable_effect: float
    method: str = "paired_t_test"

    def as_plain_english(self) -> str:
        """Render the result as validator-facing prose for a validation memo.

        The win rate and effect size are stated with an explicit direction so a
        non-statistician reader cannot misread them: Branch A is the baseline and
        Branch B is the challenger. A win rate above 50%, or a positive Cohen's d,
        favours the baseline; below 50% / negative favours the challenger.
        """
        magnitude = _effect_magnitude(self.effect_size)
        favours = "Branch A (baseline)" if self.effect_size >= 0 else "Branch B (challenger)"
        method_label = {
            "paired_t_test": "paired t-test",
            "welch_t_test": "Welch's independent-samples t-test",
        }.get(self.method, self.method)
        sig = (
            f"statistically significant (p = {self.adjusted_p_value:.3f})"
            if self.is_significant
            else f"not statistically significant (p = {self.adjusted_p_value:.3f})"
        )
        powered = (
            "the sample is adequately powered for this effect"
            if abs(self.effect_size) >= self.minimum_detectable_effect
            else "the sample may be underpowered for an effect of this size"
        )
        return (
            f"Win rate for Branch A (baseline) over Branch B (challenger): "
            f"{self.win_rate * 100:.1f}% "
            f"(95% CI: {self.ci_lower * 100:.1f}%-{self.ci_upper * 100:.1f}%); "
            f"above 50% favours the baseline, below 50% favours the challenger. "
            f"The difference is {sig} ({method_label}), with Cohen's d = "
            f"{self.effect_size:.2f} ({magnitude} effect; positive favours the "
            f"baseline, so this result favours {favours}). "
            f"Sample size: {self.sample_size} paired comparisons; {powered}."
        )


def _effect_magnitude(d: float) -> str:
    """Classify |Cohen's d| using Cohen's (1988) conventional thresholds."""
    ad = abs(d)
    if ad < 0.2:
        return "negligible"
    if ad < 0.5:
        return "small"
    if ad < 0.8:
        return "medium"
    return "large"


def _validate_pair(
    a: FloatVector, b: FloatVector
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Validate and convert a paired-score input to two float arrays.

    Raises:
        ValueError: If the inputs differ in length or contain fewer than two
            paired observations (a t-test / variance estimate is undefined below
            two samples).
    """
    if len(a) != len(b):
        raise ValueError(
            f"Branch score lists must be the same length; got {len(a)} and {len(b)}."
        )
    if len(a) < 2:
        raise ValueError("At least two paired samples are required.")
    arr_a = np.asarray(a, dtype=np.float64)
    arr_b = np.asarray(b, dtype=np.float64)
    return arr_a, arr_b


def wilson_score_interval(
    successes: float, n: int, confidence_level: float = _DEFAULT_CONFIDENCE
) -> tuple[float, float]:
    """Wilson score confidence interval for a binomial proportion.

    The Wilson interval has markedly better small-sample coverage than the naive
    Wald (normal-approximation) interval, especially for proportions near 0 or 1,
    which is exactly the regime win rates tend to occupy (Brown, Cai & DasGupta,
    2001; Wilson, 1927).

    Formula, with p_hat = successes / n and z the standard-normal quantile:

        center = (p_hat + z^2 / 2n) / (1 + z^2 / n)
        margin = (z / (1 + z^2 / n)) * sqrt(p_hat(1 - p_hat)/n + z^2 / 4n^2)

    Args:
        successes:        Number of successes (may be fractional when ties are
                          split as one half).
        n:                Number of trials (> 0).
        confidence_level: Two-sided confidence level (default 0.95).

    Returns:
        (lower, upper), each clamped to [0, 1].

    Raises:
        ValueError: If ``n`` <= 0 or ``successes`` is outside [0, n].
    """
    if n <= 0:
        raise ValueError("n must be positive.")
    if not 0.0 <= successes <= n:
        raise ValueError("successes must be within [0, n].")

    p_hat = successes / n
    z = float(stats.norm.ppf(1.0 - (1.0 - confidence_level) / 2.0))
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p_hat + z2 / (2.0 * n)) / denom
    margin = (z / denom) * math.sqrt(p_hat * (1.0 - p_hat) / n + z2 / (4.0 * n * n))
    return max(0.0, center - margin), min(1.0, center + margin)


def welch_t_test(a: FloatVector, b: FloatVector) -> tuple[float, float]:
    """Two-sided Welch's t-test (unequal variances) comparing two samples.

    Welch's t-test does not assume equal variances or equal group sizes, making
    it the robust default for comparing two model branches (Welch, 1947).

    Returns:
        (t_statistic, p_value). If both samples are constant (zero variance) the
        statistic is undefined; we return p=1.0 when the means coincide and
        p=0.0 otherwise, so degenerate inputs never yield NaN.
    """
    arr_a, arr_b = _validate_pair(a, b)
    if arr_a.var(ddof=1) == 0.0 and arr_b.var(ddof=1) == 0.0:
        means_equal = bool(np.isclose(arr_a.mean(), arr_b.mean()))
        return (0.0, 1.0) if means_equal else (math.inf, 0.0)

    result = stats.ttest_ind(arr_a, arr_b, equal_var=False)
    t_stat = float(result.statistic)
    p_value = float(result.pvalue)
    if math.isnan(p_value):  # pragma: no cover - guarded by the zero-var branch
        p_value = 1.0
    return t_stat, p_value


def cohens_d(a: FloatVector, b: FloatVector) -> float:
    """Cohen's d effect size using the pooled standard deviation (Cohen, 1988).

        d = (mean_a - mean_b) / s_pooled
        s_pooled = sqrt(((n_a-1) s_a^2 + (n_b-1) s_b^2) / (n_a + n_b - 2))

    A positive value favours Branch A. Returns 0.0 when the pooled standard
    deviation is zero (both branches perfectly constant).
    """
    arr_a, arr_b = _validate_pair(a, b)
    n_a, n_b = arr_a.size, arr_b.size
    var_a = float(arr_a.var(ddof=1))
    var_b = float(arr_b.var(ddof=1))
    pooled_var = ((n_a - 1) * var_a + (n_b - 1) * var_b) / (n_a + n_b - 2)
    pooled_sd = math.sqrt(pooled_var)
    if pooled_sd == 0.0:
        return 0.0
    return float(arr_a.mean() - arr_b.mean()) / pooled_sd


def paired_t_test(a: FloatVector, b: FloatVector) -> tuple[float, float]:
    """Two-sided paired-samples t-test on the per-sample differences a[i] - b[i].

    This is the correct significance test for ForkMark's design, where
    ``scores_a[i]`` and ``scores_b[i]`` are the two branches' scores on the *same*
    evaluation sample ``i``. Pairing removes between-sample variance, so it is
    more powerful than an independent-samples test when the samples are matched
    (Student, 1908; Cohen, 1988).

    Returns:
        (t_statistic, p_value). If every paired difference is identical (zero
        variance of differences) the statistic is undefined; we return p=1.0 when
        the mean difference is zero and p=0.0 otherwise, so degenerate inputs
        never yield NaN.
    """
    arr_a, arr_b = _validate_pair(a, b)
    diff = arr_a - arr_b
    if diff.var(ddof=1) == 0.0:
        return (0.0, 1.0) if bool(np.isclose(diff.mean(), 0.0)) else (math.inf, 0.0)
    result = stats.ttest_rel(arr_a, arr_b)
    t_stat = float(result.statistic)
    p_value = float(result.pvalue)
    if math.isnan(p_value):  # pragma: no cover - guarded by the zero-var branch
        p_value = 1.0
    return t_stat, p_value


def paired_cohens_d(a: FloatVector, b: FloatVector) -> float:
    """Cohen's d_z for paired samples (Cohen, 1988; Lakens, 2013).

        d_z = mean(a - b) / sd(a - b)

    where ``sd`` is the sample standard deviation of the per-sample differences.
    This is the effect size that matches the paired t-test: it is standardised by
    the variability of the *differences*, not the pooled between-group SD. A
    positive value favours Branch A. Returns 0.0 when every difference is
    identical (zero difference-SD).
    """
    arr_a, arr_b = _validate_pair(a, b)
    diff = arr_a - arr_b
    sd = float(diff.std(ddof=1))
    if sd == 0.0:
        return 0.0
    return float(diff.mean()) / sd


def benjamini_hochberg(p_values: Sequence[float]) -> list[float]:
    """Benjamini-Hochberg false-discovery-rate adjusted p-values (1995).

    Given m raw p-values ordered p_(1) <= ... <= p_(m), the BH step-up adjustment
    is q_(i) = min_{k >= i} ( m / k * p_(k) ), clamped to [0, 1]. Comparing the
    returned values against alpha controls the expected proportion of false
    discoveries among the rejected hypotheses at level alpha.

    Args:
        p_values: Raw p-values.

    Returns:
        FDR-adjusted p-values in the original input order.

    Raises:
        ValueError: If ``p_values`` is empty.
    """
    m = len(p_values)
    if m == 0:
        raise ValueError("p_values must be non-empty.")
    raw = np.asarray(p_values, dtype=np.float64)
    order = np.argsort(raw)
    ranked = raw[order]
    ranks = np.arange(1, m + 1, dtype=np.float64)
    adjusted = ranked * m / ranks
    # Enforce monotonicity by taking the running minimum from largest rank down.
    adjusted = np.minimum.accumulate(adjusted[::-1])[::-1]
    adjusted = np.clip(adjusted, 0.0, 1.0)
    out = np.empty(m, dtype=np.float64)
    out[order] = adjusted
    return [float(x) for x in out]


def minimum_detectable_effect(
    n_per_group: int,
    power: float = _DEFAULT_POWER,
    alpha: float = _DEFAULT_ALPHA,
    paired: bool = False,
) -> float:
    """Smallest Cohen's d detectable by a t-test at the given n.

    Inverting the standard sample-size relation (Cohen, 1988) under a normal
    approximation:

        independent (default): d_min  = (z_{1-alpha/2} + z_{power}) * sqrt(2 / n)
        paired:                d_z_min = (z_{1-alpha/2} + z_{power}) * sqrt(1 / n)

    The paired design detects smaller effects at the same n because it removes
    between-sample variance; the returned value is then a Cohen's d_z, matching
    the paired effect size used by :func:`analyze`.

    Args:
        n_per_group: Paired samples (paired) or samples per branch (independent), > 0.
        power:       Desired statistical power (default 0.80).
        alpha:       Two-sided significance level (default 0.05).
        paired:      Use the paired (d_z) relation when True (default False).

    Returns:
        The minimum detectable effect size (Cohen's d, or d_z when ``paired``).

    Raises:
        ValueError: If ``n_per_group`` <= 0.
    """
    if n_per_group <= 0:
        raise ValueError("n_per_group must be positive.")
    z_alpha = float(stats.norm.ppf(1.0 - alpha / 2.0))
    z_power = float(stats.norm.ppf(power))
    factor = 1.0 if paired else 2.0
    return (z_alpha + z_power) * math.sqrt(factor / n_per_group)


def power_analysis(
    effect_size: float, power: float = _DEFAULT_POWER, alpha: float = _DEFAULT_ALPHA
) -> int:
    """Minimum samples *per branch* to detect ``effect_size`` at the given power.

    Standard two-sample sample-size formula (Cohen, 1988), normal approximation:

        n_per_group = 2 * ((z_{1-alpha/2} + z_{power}) / d)^2

    The normal approximation slightly underestimates n for very small samples; it
    is the conventional closed form and is rounded up to the next whole sample.

    Args:
        effect_size: Target Cohen's d to detect (non-zero; sign ignored).
        power:       Desired statistical power (default 0.80).
        alpha:       Two-sided significance level (default 0.05).

    Returns:
        Minimum number of samples per branch (>= 2).

    Raises:
        ValueError: If ``effect_size`` is zero, or power/alpha are out of range.
    """
    if effect_size == 0.0:
        raise ValueError("effect_size must be non-zero.")
    if not 0.0 < power < 1.0:
        raise ValueError("power must be in (0, 1).")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1).")
    d = abs(effect_size)
    z_alpha = float(stats.norm.ppf(1.0 - alpha / 2.0))
    z_power = float(stats.norm.ppf(power))
    n = 2.0 * ((z_alpha + z_power) / d) ** 2
    return max(2, math.ceil(n))


def _win_rate_successes(
    arr_a: npt.NDArray[np.float64],
    arr_b: npt.NDArray[np.float64],
) -> float:
    """Count Branch A wins, splitting ties as one half (standard win-rate rule)."""
    wins = float(np.sum(arr_a > arr_b))
    ties = float(np.sum(arr_a == arr_b))
    return wins + 0.5 * ties


def analyze(
    scores_a: FloatVector,
    scores_b: FloatVector,
    *,
    confidence_level: float = _DEFAULT_CONFIDENCE,
    alpha: float = _DEFAULT_ALPHA,
    paired: bool = True,
) -> StatisticalResult:
    """Compute the full statistical summary for one A/B comparison.

    ForkMark's evaluation design is *paired*: ``scores_a[i]`` and ``scores_b[i]``
    are the two branches' scores on the same evaluation sample ``i``. The win rate
    is therefore always computed pairwise, and — by default — so is the
    significance test (a paired t-test) and the effect size (Cohen's d_z). This
    keeps the whole summary internally consistent on the paired basis. Set
    ``paired=False`` only when the two score sets come from genuinely independent
    samples, in which case a Welch independent-samples t-test with pooled Cohen's
    d is used instead.

    Args:
        scores_a:         Per-sample quality scores for Branch A (higher = better).
        scores_b:         Per-sample quality scores for Branch B, paired with A.
        confidence_level: Confidence level for the win-rate interval (default 0.95).
        alpha:            Significance level for ``is_significant`` (default 0.05).
        paired:           Treat the samples as matched pairs (default True).

    Returns:
        A :class:`StatisticalResult`. For a single comparison the
        ``adjusted_p_value`` equals the raw ``p_value``; use :func:`analyze_batch`
        to apply multiple-comparison control across several comparisons.
    """
    arr_a, arr_b = _validate_pair(scores_a, scores_b)
    n = int(arr_a.size)

    successes = _win_rate_successes(arr_a, arr_b)
    win_rate = successes / n
    ci_lower, ci_upper = wilson_score_interval(successes, n, confidence_level)
    if paired:
        _, p_value = paired_t_test(arr_a, arr_b)
        effect_size = paired_cohens_d(arr_a, arr_b)
        mde = minimum_detectable_effect(n, power=_DEFAULT_POWER, alpha=alpha, paired=True)
        method = "paired_t_test"
    else:
        _, p_value = welch_t_test(arr_a, arr_b)
        effect_size = cohens_d(arr_a, arr_b)
        mde = minimum_detectable_effect(n, power=_DEFAULT_POWER, alpha=alpha, paired=False)
        method = "welch_t_test"

    return StatisticalResult(
        win_rate=win_rate,
        ci_lower=ci_lower,
        ci_upper=ci_upper,
        p_value=p_value,
        adjusted_p_value=p_value,
        effect_size=effect_size,
        is_significant=p_value < alpha,
        sample_size=n,
        minimum_detectable_effect=mde,
        method=method,
    )


def analyze_batch(
    comparisons: Sequence[tuple[FloatVector, FloatVector]],
    *,
    confidence_level: float = _DEFAULT_CONFIDENCE,
    alpha: float = _DEFAULT_ALPHA,
    paired: bool = True,
) -> list[StatisticalResult]:
    """Analyze several comparisons and apply Benjamini-Hochberg FDR control.

    Running many comparisons inflates the family-wise false-positive rate; the BH
    procedure controls the false discovery rate across the batch. Each returned
    result carries its BH-adjusted p-value, and ``is_significant`` is decided on
    the adjusted value.

    Args:
        comparisons:      Sequence of ``(scores_a, scores_b)`` paired-score tuples.
        confidence_level: Confidence level for each win-rate interval.
        alpha:            FDR level and significance threshold.

    Returns:
        One :class:`StatisticalResult` per input comparison, in input order.

    Raises:
        ValueError: If ``comparisons`` is empty.
    """
    if not comparisons:
        raise ValueError("comparisons must be non-empty.")

    base = [
        analyze(a, b, confidence_level=confidence_level, alpha=alpha, paired=paired)
        for a, b in comparisons
    ]
    adjusted = benjamini_hochberg([r.p_value for r in base])
    logger.debug(
        "analyze_batch: %d comparisons, %d significant after BH at alpha=%.3f",
        len(base),
        sum(1 for q in adjusted if q < alpha),
        alpha,
    )
    return [
        dataclasses.replace(
            result,
            adjusted_p_value=adj_p,
            is_significant=adj_p < alpha,
        )
        for result, adj_p in zip(base, adjusted)
    ]
