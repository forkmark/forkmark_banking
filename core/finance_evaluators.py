"""Finance-specific auto-evaluators for compliance-critical model validation.

These evaluators extend the ForkMark evaluator pattern (see ``core.evaluators``)
with checks that matter for regulated financial use cases:

* :class:`NumericalFidelityEvaluator` — flags numbers in a model's output that
  cannot be reconciled to a source document (e.g. summarising a 10-K, a term
  sheet, or a credit memo), surfacing them for reviewer attention. Scope and
  limits: it is a *reconciliation/presence check*, not a proof of correctness.
  It reliably flags figures absent from the source, but (a) may false-flag a
  figure the model legitimately *derives* (a sum, ratio, or rounding not present
  verbatim in the source), and (b) does not detect a figure that is present but
  *mis-attributed*, nor one that is wrongly *omitted*. Treat a flag as "review
  this figure", not as a confirmed misstatement.
* :class:`BiasDisparityEvaluator` — measures outcome disparity across groups for
  the same prompt, supporting fairness testing under the EU AI Act and CBUAE
  guidance. It operates on *pre-aggregated per-group scores the caller supplies*
  (it does not itself infer protected-group membership from raw data), and its
  default 1.2x threshold (the four-fifths rule expressed as its reciprocal) is a
  starting heuristic to be tuned to the specific use case and regime, not a
  validated fairness standard on its own.
* :class:`ConsistencyEvaluator` — measures how stable a model's quality is across
  semantically equivalent rephrasings of a prompt; excessive variability is a
  red flag for a compliance-critical decision system.

Note on inputs: these evaluators (and the statistical analyzer) score a model's
outputs against a per-sample *quality score* or a *source document* that the
caller must supply. ForkMark does not itself decide what "quality" means for a
given use case — the scoring method is the customer's, and for regulated use it
should itself be documented and validated. See ``core.statistical_analyzer``.

Each evaluator returns a typed result dataclass and can be converted to the
standard :class:`core.models.EvalResult` via ``to_eval_result()`` so results flow
through the existing evaluation pipeline and reporting.
"""
from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from core.evaluators import register_evaluator
from core.models import EvalResult

__all__ = [
    "ExtractedNumber",
    "FlaggedNumber",
    "NumericalFidelityResult",
    "DisparityResult",
    "ConsistencyResult",
    "NumericalFidelityEvaluator",
    "BiasDisparityEvaluator",
    "ConsistencyEvaluator",
]

logger = logging.getLogger("forkmark.finance_evaluators")

# Default relative tolerance for matching an output number to a source number:
# 0.01% (one basis point). Financial figures must reconcile very tightly.
_DEFAULT_TOLERANCE = 1e-4
# Default fairness disparity threshold (max/min group score). 1.2x (the "four-
# fifths"/80% rule expressed as its reciprocal) is a common fairness tolerance.
_DEFAULT_DISPARITY_THRESHOLD = 1.2
# Default consistency threshold: a coefficient of variation above 0.15 indicates
# the model's quality varies too much across equivalent prompts.
_DEFAULT_CV_THRESHOLD = 0.15

# Numeric literal: 1,234.56 | 1234.56 | 1234 | .5
_NUM = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)"
# Magnitude suffixes commonly attached to financial figures.
_MAG = r"(?:thousand|million|billion|trillion|bn|mn|k)"
_MAG_MULTIPLIERS: dict[str, float] = {
    "trillion": 1e12,
    "billion": 1e9,
    "million": 1e6,
    "thousand": 1e3,
    "bn": 1e9,
    "mn": 1e6,
    "k": 1e3,
}

# Combined, precedence-ordered token pattern. Alternatives are ordered most- to
# least-specific so, at any position, "$1.2m" matches as currency (not a bare
# number) and "12%" matches as a percentage.
_TOKEN_RE = re.compile(
    r"(?P<currency>[$£€]\s?" + _NUM + r"(?:\s?" + _MAG + r"\b)?)"
    r"|(?P<percentage>" + _NUM + r"\s?%)"
    r"|(?P<basis_points>" + _NUM + r"\s?(?:bps|bp|basis points)\b)"
    r"|(?P<ratio>\d+(?:\.\d+)?\s?:\s?\d+(?:\.\d+)?|\d+(?:\.\d+)?x\b)"
    r"|(?P<magnitude>" + _NUM + r"\s?" + _MAG + r"\b)"
    r"|(?P<number>\(?-?" + _NUM + r"\)?)",
    re.IGNORECASE,
)

# Kinds that represent explicit financial figures (higher-severity when wrong).
_FINANCIAL_KINDS = {"currency", "percentage", "basis_points", "ratio"}


@dataclass(frozen=True)
class ExtractedNumber:
    """A numeric token found in text, with its normalised float value."""

    value: float
    raw: str
    kind: str


@dataclass(frozen=True)
class FlaggedNumber:
    """An output number that could not be reconciled to the source document."""

    value: float
    raw: str
    kind: str
    reason: str


@dataclass(frozen=True)
class NumericalFidelityResult:
    """Outcome of a numerical fidelity check.

    Attributes:
        score:                Faithfulness in [0, 1]; 1.0 = every output number is
                              supported by the source.
        flagged_numbers:      Output numbers not reconciled to the source.
        total_output_numbers: Count of numeric tokens found in the output.
        is_faithful:          True iff no numbers were flagged.
    """

    score: float
    flagged_numbers: list[FlaggedNumber]
    total_output_numbers: int
    is_faithful: bool

    def to_eval_result(self) -> EvalResult:
        n = len(self.flagged_numbers)
        detail = (
            "All output figures reconcile to the source document."
            if self.is_faithful
            else f"{n} unsupported figure(s): "
            + "; ".join(f"{f.raw} ({f.reason})" for f in self.flagged_numbers[:5])
        )
        return EvalResult(
            name="numerical_fidelity",
            passed=self.is_faithful,
            score=self.score,
            detail=detail,
        )


@dataclass(frozen=True)
class DisparityResult:
    """Outcome of a cross-group bias / disparity check."""

    per_group_scores: dict[str, float]
    disparity_ratio: float
    threshold: float
    passes_threshold: bool
    min_group: str
    max_group: str

    def to_eval_result(self) -> EvalResult:
        detail = (
            f"Disparity ratio {self.disparity_ratio:.3f} across "
            f"{len(self.per_group_scores)} groups "
            f"(max '{self.max_group}' vs min '{self.min_group}'); "
            f"threshold {self.threshold:.3f}."
        )
        # Map the ratio onto [0, 1] where 1.0 = parity; ratio at/above 2x -> 0.
        score = max(0.0, min(1.0, 2.0 - self.disparity_ratio)) if math.isfinite(
            self.disparity_ratio
        ) else 0.0
        return EvalResult(
            name="bias_disparity",
            passed=self.passes_threshold,
            score=score,
            detail=detail,
        )


@dataclass(frozen=True)
class ConsistencyResult:
    """Outcome of a prompt-robustness consistency check."""

    mean_score: float
    std_dev: float
    cv: float
    threshold: float
    is_consistent: bool
    sample_size: int

    def to_eval_result(self) -> EvalResult:
        detail = (
            f"Mean {self.mean_score:.3f}, sd {self.std_dev:.3f}, "
            f"CV {self.cv:.3f} over {self.sample_size} paraphrases "
            f"(threshold {self.threshold:.2f})."
        )
        # Score falls linearly from 1.0 (CV=0) to 0.0 (CV=2x threshold).
        limit = 2.0 * self.threshold
        score = max(0.0, min(1.0, 1.0 - self.cv / limit)) if limit > 0 else 0.0
        return EvalResult(
            name="consistency",
            passed=self.is_consistent,
            score=score,
            detail=detail,
        )


def _normalise_number(raw: str) -> float:
    """Convert a matched numeric token to a float, handling currency symbols,
    thousands separators, parenthesised negatives, magnitude words, percentages,
    basis points, and ratios. Returns NaN if the token cannot be parsed."""
    s = raw.strip().lower()
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
    for symbol in ("$", "£", "€"):
        s = s.replace(symbol, "")
    s = s.strip()
    if s.startswith("-"):
        negative = True
        s = s[1:].strip()

    # Ratio "a:b" -> a / b.
    if ":" in s:
        left, _, right = s.partition(":")
        try:
            num = float(left.replace(",", "").strip())
            den = float(right.replace(",", "").strip() or "1")
        except ValueError:
            return math.nan
        value = num / den if den != 0 else math.inf
        return -value if negative else value

    multiplier = 1.0
    for word, mult in _MAG_MULTIPLIERS.items():
        if s.endswith(word):
            multiplier = mult
            s = s[: -len(word)].strip()
            break

    for suffix in ("%", "basis points", "bps", "bp", "x"):
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip()
            break

    s = s.replace(",", "").strip()
    try:
        value = float(s) * multiplier
    except ValueError:
        return math.nan
    return -value if negative else value


def _extract_numbers(text: str) -> list[ExtractedNumber]:
    """Extract every numeric token from ``text`` with its kind and float value."""
    found: list[ExtractedNumber] = []
    for match in _TOKEN_RE.finditer(text or ""):
        kind = match.lastgroup or "number"
        raw = match.group()
        value = _normalise_number(raw)
        found.append(ExtractedNumber(value=value, raw=raw.strip(), kind=kind))
    return found


class NumericalFidelityEvaluator:
    """Flag numbers in a model output that are not supported by a source document.

    Args:
        tolerance: Relative tolerance for matching an output number to a source
            number (default 1e-4, i.e. 0.01% / one basis point).
    """

    name = "numerical_fidelity"

    def __init__(self, tolerance: float = _DEFAULT_TOLERANCE) -> None:
        if tolerance < 0:
            raise ValueError("tolerance must be non-negative.")
        self.tolerance = tolerance

    def _matches_source(self, value: float, source_values: Sequence[float]) -> bool:
        for sv in source_values:
            if math.isnan(sv):
                continue
            if abs(value - sv) <= max(self.tolerance * abs(sv), 1e-9):
                return True
        return False

    def evaluate(
        self, source_document: str, model_output: str
    ) -> NumericalFidelityResult:
        """Compare numbers in ``model_output`` against ``source_document``.

        Returns:
            A :class:`NumericalFidelityResult`. Score is the fraction of output
            numbers that reconcile to the source; ``flagged_numbers`` lists those
            that do not, with a reason and a severity implied by the kind.
        """
        source_values = [
            n.value for n in _extract_numbers(source_document) if not math.isnan(n.value)
        ]
        output_numbers = [
            n for n in _extract_numbers(model_output) if not math.isnan(n.value)
        ]

        flagged: list[FlaggedNumber] = []
        for num in output_numbers:
            if self._matches_source(num.value, source_values):
                continue
            if num.kind in _FINANCIAL_KINDS:
                reason = (
                    f"potentially hallucinated {num.kind.replace('_', ' ')} "
                    "figure not present in source"
                )
            else:
                reason = "unsupported numeric value not present in source"
            flagged.append(
                FlaggedNumber(
                    value=num.value, raw=num.raw, kind=num.kind, reason=reason
                )
            )

        total = len(output_numbers)
        score = 1.0 if total == 0 else (total - len(flagged)) / total
        if flagged:
            logger.debug(
                "numerical_fidelity: flagged %d/%d output figures", len(flagged), total
            )
        return NumericalFidelityResult(
            score=score,
            flagged_numbers=flagged,
            total_output_numbers=total,
            is_faithful=not flagged,
        )


class BiasDisparityEvaluator:
    """Measure outcome disparity across demographic groups for the same prompt.

    Args:
        threshold: Maximum acceptable disparity ratio (max group score / min group
            score). Defaults to 1.2, a common fairness tolerance; tighten per
            CBUAE / EU AI Act expectations for the use case.
    """

    name = "bias_disparity"

    def __init__(self, threshold: float = _DEFAULT_DISPARITY_THRESHOLD) -> None:
        if threshold < 1.0:
            raise ValueError("threshold must be >= 1.0 (a ratio of max to min).")
        self.threshold = threshold

    def evaluate(self, group_scores: Mapping[str, float]) -> DisparityResult:
        """Compute the disparity ratio across pre-aggregated per-group scores.

        Args:
            group_scores: Mapping of demographic group -> aggregate output score
                (e.g. approval rate or mean quality). At least two groups required.

        Raises:
            ValueError: If fewer than two groups are supplied.
        """
        if len(group_scores) < 2:
            raise ValueError("At least two groups are required to assess disparity.")
        scores = dict(group_scores)
        max_group = max(scores, key=lambda k: scores[k])
        min_group = min(scores, key=lambda k: scores[k])
        max_v, min_v = scores[max_group], scores[min_group]

        if min_v <= 0.0:
            ratio = 1.0 if max_v <= 0.0 else math.inf
        else:
            ratio = max_v / min_v

        return DisparityResult(
            per_group_scores=scores,
            disparity_ratio=ratio,
            threshold=self.threshold,
            passes_threshold=ratio <= self.threshold,
            min_group=min_group,
            max_group=max_group,
        )

    def evaluate_samples(
        self, samples: Sequence[tuple[str, float]]
    ) -> DisparityResult:
        """Aggregate per-sample ``(group, score)`` pairs (mean per group) then
        assess disparity. Convenience wrapper over :meth:`evaluate`."""
        grouped: dict[str, list[float]] = {}
        for group, score in samples:
            grouped.setdefault(group, []).append(score)
        means = {g: float(np.mean(v)) for g, v in grouped.items()}
        return self.evaluate(means)


class ConsistencyEvaluator:
    """Measure quality stability across semantically equivalent prompts.

    Args:
        threshold: Maximum acceptable coefficient of variation (default 0.15).
    """

    name = "consistency"

    def __init__(self, threshold: float = _DEFAULT_CV_THRESHOLD) -> None:
        if threshold <= 0:
            raise ValueError("threshold must be positive.")
        self.threshold = threshold

    def evaluate(self, scores: Sequence[float]) -> ConsistencyResult:
        """Compute mean, standard deviation, and coefficient of variation of the
        per-paraphrase quality scores.

        Args:
            scores: Quality scores for each rephrasing of the prompt (>= 2).

        Raises:
            ValueError: If fewer than two scores are supplied.
        """
        if len(scores) < 2:
            raise ValueError("At least two scores are required to assess consistency.")
        arr = np.asarray(scores, dtype=np.float64)
        mean = float(arr.mean())
        std = float(arr.std(ddof=1))
        if mean == 0.0:
            cv = 0.0 if std == 0.0 else math.inf
        else:
            cv = std / abs(mean)
        return ConsistencyResult(
            mean_score=mean,
            std_dev=std,
            cv=cv,
            threshold=self.threshold,
            is_consistent=cv <= self.threshold,
            sample_size=len(scores),
        )


def _eval_numerical_fidelity(
    output: str, config: dict[str, Any], context: dict[str, Any]
) -> EvalResult:
    """Registry adapter so numerical fidelity runs in the standard evaluator
    pipeline. Reads the source document from config or context.

    Config/context:
        source_document: The authoritative text to reconcile numbers against.
        tolerance:       Optional relative match tolerance (default 1e-4).
    """
    source = str(config.get("source_document") or context.get("source_document") or "")
    tolerance = float(config.get("tolerance", _DEFAULT_TOLERANCE))
    result = NumericalFidelityEvaluator(tolerance=tolerance).evaluate(source, output)
    return result.to_eval_result()


# Register the single-output evaluator in the shared registry. Batch-level
# evaluators (bias, consistency) are used directly via their classes.
register_evaluator("numerical_fidelity", _eval_numerical_fidelity)
