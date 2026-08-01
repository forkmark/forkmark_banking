"""Unit tests for core.statistical_analyzer."""
from __future__ import annotations

import math

import pytest

from core.statistical_analyzer import (
    analyze,
    analyze_batch,
    benjamini_hochberg,
    cohens_d,
    minimum_detectable_effect,
    paired_cohens_d,
    paired_t_test,
    power_analysis,
    welch_t_test,
    wilson_score_interval,
)


def test_wilson_interval_matches_reference_value() -> None:
    """Wilson 95% interval for 8/10 matches the textbook value (~0.490-0.943)."""
    lower, upper = wilson_score_interval(8, 10, 0.95)
    assert lower == pytest.approx(0.490, abs=0.01)
    assert upper == pytest.approx(0.943, abs=0.01)
    assert 0.0 <= lower < 0.8 < upper <= 1.0


def test_wilson_interval_is_bounded_and_validated() -> None:
    """Interval is clamped to [0, 1] at extremes and rejects bad inputs."""
    lower, upper = wilson_score_interval(10, 10, 0.95)
    assert 0.0 <= lower <= 1.0 and upper == pytest.approx(1.0, abs=1e-9)
    with pytest.raises(ValueError):
        wilson_score_interval(5, 0)
    with pytest.raises(ValueError):
        wilson_score_interval(11, 10)


def test_analyze_flags_significant_when_branch_a_dominates() -> None:
    """When A beats B on every sample, win_rate is 1.0 and the difference is
    significant with a large positive effect size."""
    scores_a = [0.90, 0.88, 0.91, 0.87, 0.93, 0.89, 0.92, 0.90, 0.88, 0.91]
    scores_b = [0.20, 0.25, 0.22, 0.24, 0.21, 0.23, 0.19, 0.26, 0.20, 0.22]
    result = analyze(scores_a, scores_b)
    assert type(result).__name__ == "StatisticalResult"
    assert result.win_rate == 1.0
    assert result.sample_size == 10
    assert result.effect_size > 0.8  # large effect (Cohen, 1988)
    assert result.p_value < 0.05
    assert result.is_significant is True
    # Containment holds up to floating-point epsilon (Wilson upper for p_hat=1
    # is mathematically 1.0 but computes to 1 - 1e-16).
    assert result.ci_lower <= result.win_rate <= result.ci_upper + 1e-9
    assert "Win rate" in result.as_plain_english()


def test_analyze_reports_no_significant_difference_for_identical_branches() -> None:
    """Identical score vectors → 50% win rate, p=1.0, zero effect, not significant."""
    scores = [0.5, 0.6, 0.55, 0.52, 0.58, 0.61]
    result = analyze(scores, list(scores))
    assert result.win_rate == pytest.approx(0.5)
    assert result.p_value == pytest.approx(1.0)
    assert result.effect_size == pytest.approx(0.0, abs=1e-12)
    assert result.is_significant is False


def test_input_validation_raises() -> None:
    """Mismatched or too-short inputs raise ValueError."""
    with pytest.raises(ValueError):
        analyze([0.1, 0.2], [0.1])
    with pytest.raises(ValueError):
        analyze([0.1], [0.2])
    with pytest.raises(ValueError):
        welch_t_test([0.1], [0.2])


def test_cohens_d_sign_and_zero_variance() -> None:
    """Cohen's d is positive when A > B and 0.0 when both branches are constant."""
    d_pos = cohens_d([1.0, 1.1, 0.9, 1.0], [0.1, 0.0, 0.2, 0.1])
    assert d_pos > 0
    assert cohens_d([0.5, 0.5, 0.5], [0.5, 0.5, 0.5]) == 0.0


def test_paired_t_test_matches_design_and_handles_degenerate() -> None:
    """The paired t-test operates on per-sample differences, validates inputs,
    and returns finite, sensible p-values including the zero-variance edge case."""
    a = [0.81, 0.79, 0.84, 0.80, 0.83, 0.78]
    b = [0.70, 0.69, 0.72, 0.68, 0.71, 0.67]  # A consistently ~0.11 above B
    t_stat, p = paired_t_test(a, b)
    assert t_stat > 0 and p < 0.05  # a real, significant paired difference
    # Identical vectors -> zero difference variance -> p = 1.0, not NaN.
    assert paired_t_test([0.5, 0.6, 0.7], [0.5, 0.6, 0.7]) == (0.0, 1.0)
    # A constant non-zero offset (exactly-representable diffs of 1.0) -> undefined
    # t, p = 0.0 (guarded, never NaN).
    assert paired_t_test([1.0, 2.0, 3.0], [0.0, 1.0, 2.0]) == (math.inf, 0.0)
    with pytest.raises(ValueError):
        paired_t_test([0.1], [0.2])


def test_paired_cohens_d_uses_difference_sd() -> None:
    """Cohen's d_z is mean(diff)/sd(diff): positive when A leads, 0 when the
    per-sample difference is constant (zero difference-SD)."""
    d = paired_cohens_d([0.9, 0.8, 0.85, 0.95], [0.2, 0.3, 0.25, 0.15])
    assert d > 0.8  # large paired effect
    # Exactly-representable constant difference of 1.0 -> zero difference-SD -> 0.0
    assert paired_cohens_d([1.0, 2.0, 3.0], [0.0, 1.0, 2.0]) == 0.0


def test_analyze_defaults_to_paired_and_reports_method() -> None:
    """analyze() is paired by default (method label + direction in the prose);
    paired=False falls back to the independent Welch test with pooled d."""
    a = [0.90, 0.88, 0.91, 0.87, 0.93, 0.89]
    b = [0.20, 0.25, 0.22, 0.24, 0.21, 0.23]
    paired = analyze(a, b)
    assert paired.method == "paired_t_test"
    assert paired.is_significant is True
    assert "baseline" in paired.as_plain_english() and "challenger" in paired.as_plain_english()

    indep = analyze(a, b, paired=False)
    assert indep.method == "welch_t_test"
    assert indep.is_significant is True

    # Identical branches stay non-significant under the paired test (no NaN).
    null = analyze([0.5, 0.6, 0.55, 0.52], [0.5, 0.6, 0.55, 0.52])
    assert null.is_significant is False
    assert null.p_value == pytest.approx(1.0)
    assert null.effect_size == pytest.approx(0.0, abs=1e-12)


def test_paired_mde_is_tighter_than_independent() -> None:
    """A paired design detects a smaller effect than an independent one at the
    same n (sqrt(1/n) vs sqrt(2/n))."""
    assert minimum_detectable_effect(50, paired=True) < minimum_detectable_effect(50)


def test_benjamini_hochberg_known_values_and_order() -> None:
    """BH adjustment matches hand-computed values and preserves input order."""
    # All p_i * m / i equal 0.05, so every adjusted value is 0.05.
    adj = benjamini_hochberg([0.01, 0.02, 0.03, 0.04, 0.05])
    assert all(a == pytest.approx(0.05, abs=1e-9) for a in adj)

    # Order preservation: input positions map back correctly.
    adj2 = benjamini_hochberg([0.9, 0.001, 0.5])
    assert adj2[1] == pytest.approx(0.003, abs=1e-9)
    assert adj2[2] == pytest.approx(0.75, abs=1e-9)
    assert adj2[0] == pytest.approx(0.9, abs=1e-9)

    with pytest.raises(ValueError):
        benjamini_hochberg([])


def test_power_analysis_and_mde_are_consistent() -> None:
    """Sample-size and minimum-detectable-effect helpers invert each other."""
    n = power_analysis(0.5, power=0.8, alpha=0.05)
    assert 60 <= n <= 68  # ~63-64 per group for a medium effect
    # Feeding that n back into MDE recovers ~0.5.
    mde = minimum_detectable_effect(n, power=0.8, alpha=0.05)
    assert mde == pytest.approx(0.5, abs=0.03)
    # Larger samples detect smaller effects.
    assert minimum_detectable_effect(1000) < minimum_detectable_effect(100)
    with pytest.raises(ValueError):
        power_analysis(0.0)


def test_analyze_batch_applies_fdr_control() -> None:
    """Batch analysis returns BH-adjusted p-values >= the raw p-values, one per
    comparison, with significance decided on the adjusted value."""
    strong_a = [0.95, 0.93, 0.94, 0.96, 0.92, 0.95, 0.94, 0.93]
    strong_b = [0.10, 0.12, 0.11, 0.09, 0.13, 0.10, 0.12, 0.11]
    null_a = [0.50, 0.51, 0.49, 0.52, 0.48, 0.50, 0.51, 0.49]
    null_b = [0.50, 0.49, 0.51, 0.48, 0.52, 0.50, 0.49, 0.51]

    results = analyze_batch([(strong_a, strong_b), (null_a, null_b), (null_a, null_b)])
    assert len(results) == 3
    for r in results:
        assert r.adjusted_p_value >= r.p_value - 1e-12
        assert r.adjusted_p_value <= 1.0
        assert r.is_significant == (r.adjusted_p_value < 0.05)
    # The strong comparison should survive correction; the null ones should not.
    assert results[0].is_significant is True
    assert results[1].is_significant is False
    assert results[2].is_significant is False

    with pytest.raises(ValueError):
        analyze_batch([])


def test_result_plain_english_mentions_power_context() -> None:
    """The plain-English summary references sample size and effect magnitude."""
    text = analyze([0.9, 0.8, 0.85, 0.95], [0.2, 0.3, 0.25, 0.15]).as_plain_english()
    assert "paired comparisons" in text
    assert "Cohen's d" in text
    assert math.isfinite(1.0)  # sanity: module imported and usable
