"""Unit tests for core.finance_evaluators."""
from __future__ import annotations

import math

import pytest

from core.finance_evaluators import (
    BiasDisparityEvaluator,
    ConsistencyEvaluator,
    NumericalFidelityEvaluator,
    _extract_numbers,
)


def test_number_extraction_handles_financial_notation() -> None:
    """The extractor recognises currency, percentages, bps, magnitudes, ratios."""
    kinds = {n.raw: n.kind for n in _extract_numbers(
        "Revenue was $3.5 million, up 12.5%, spread 25 bps, leverage 3:1."
    )}
    assert any(k == "currency" for k in kinds.values())
    assert any(k == "percentage" for k in kinds.values())
    assert any(k == "basis_points" for k in kinds.values())
    # $3.5 million normalises to 3,500,000.
    cur = next(n for n in _extract_numbers("$3.5 million") if n.kind == "currency")
    assert cur.value == pytest.approx(3_500_000.0)


def test_numerical_fidelity_passes_when_figures_reconcile() -> None:
    """An output whose numbers all appear in the source is fully faithful."""
    source = "Net income was $4.2 million on revenue of $10 million, a 42% margin."
    output = "The company earned $4.2 million, a 42% margin."
    result = NumericalFidelityEvaluator().evaluate(source, output)
    assert result.is_faithful is True
    assert result.score == 1.0
    assert result.flagged_numbers == []


def test_numerical_fidelity_flags_hallucinated_figure() -> None:
    """A fabricated figure not present in the source is flagged with its kind."""
    source = "Net income was $4.2 million, a 42% margin."
    output = "Net income was $4.2 million, a 58% margin, up 15 bps."
    result = NumericalFidelityEvaluator().evaluate(source, output)
    assert result.is_faithful is False
    assert result.score < 1.0
    flagged_values = {round(f.value, 4) for f in result.flagged_numbers}
    assert 58.0 in flagged_values  # the invented 58% margin
    assert any("hallucinated" in f.reason for f in result.flagged_numbers)
    # Converts cleanly to a standard EvalResult.
    er = result.to_eval_result()
    assert er.name == "numerical_fidelity" and er.passed is False


def test_numerical_fidelity_tolerance_allows_rounding() -> None:
    """A figure within the relative tolerance is treated as reconciled."""
    source = "Total exposure is 1000000."
    output = "Total exposure is about 1000000."
    assert NumericalFidelityEvaluator(tolerance=1e-3).evaluate(source, output).is_faithful


def test_bias_disparity_detects_and_passes() -> None:
    """Disparity ratio = max/min group score; flagged when above threshold."""
    ev = BiasDisparityEvaluator(threshold=1.2)
    fair = ev.evaluate({"group_a": 0.80, "group_b": 0.78, "group_c": 0.82})
    assert fair.passes_threshold is True
    assert fair.disparity_ratio == pytest.approx(0.82 / 0.78, rel=1e-6)

    biased = ev.evaluate({"group_a": 0.90, "group_b": 0.50})
    assert biased.passes_threshold is False
    assert biased.max_group == "group_a" and biased.min_group == "group_b"
    assert biased.disparity_ratio == pytest.approx(1.8)

    with pytest.raises(ValueError):
        ev.evaluate({"only_one_group": 0.5})


def test_bias_disparity_from_samples_aggregates_means() -> None:
    """evaluate_samples averages per-group scores before computing the ratio."""
    ev = BiasDisparityEvaluator(threshold=1.5)
    result = ev.evaluate_samples(
        [("male", 0.8), ("male", 0.9), ("female", 0.4), ("female", 0.4)]
    )
    assert result.per_group_scores["male"] == pytest.approx(0.85)
    assert result.per_group_scores["female"] == pytest.approx(0.40)
    assert result.disparity_ratio == pytest.approx(0.85 / 0.40)
    assert result.passes_threshold is False


def test_consistency_flags_high_variability() -> None:
    """CV above the threshold marks the model as inconsistent across paraphrases."""
    stable = ConsistencyEvaluator().evaluate([0.80, 0.81, 0.79, 0.80, 0.82])
    assert stable.is_consistent is True
    assert stable.cv < 0.15

    volatile = ConsistencyEvaluator().evaluate([0.9, 0.2, 0.7, 0.1, 0.95])
    assert volatile.is_consistent is False
    assert volatile.cv > 0.15
    assert volatile.sample_size == 5

    with pytest.raises(ValueError):
        ConsistencyEvaluator().evaluate([0.5])


def test_consistency_handles_zero_mean() -> None:
    """A zero-mean, zero-variance input has CV 0 (perfectly consistent)."""
    result = ConsistencyEvaluator().evaluate([0.0, 0.0, 0.0])
    assert result.cv == 0.0
    assert result.is_consistent is True
    assert math.isfinite(result.std_dev)


def test_numerical_fidelity_registered_in_pipeline() -> None:
    """Importing the module registers 'numerical_fidelity' in the shared registry."""
    import asyncio

    from core.evaluators import run_evaluators

    try:
        results = asyncio.run(
            run_evaluators(
                "The margin was 99%.",
                [{"name": "numerical_fidelity",
                  "source_document": "The margin was 42%."}],
            )
        )
    finally:
        # asyncio.run() closes the loop; restore one so later tests that use the
        # legacy asyncio.get_event_loop() pattern still find a current loop.
        asyncio.set_event_loop(asyncio.new_event_loop())
    assert results[0].name == "numerical_fidelity"
    assert results[0].passed is False
