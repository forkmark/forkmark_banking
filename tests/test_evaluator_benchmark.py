"""Regression guard for the evaluator-accuracy benchmark.

Runs the benchmark in-process and asserts the measured metrics hold at or above
their documented floors, so a regression in the numerical-fidelity or disparity
evaluators (or a corrupted dataset) fails CI rather than silently degrading the
"validate the validator" numbers. See benchmarks/evaluator_accuracy/.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_BENCH = Path(__file__).resolve().parents[1] / "benchmarks" / "evaluator_accuracy"
_spec = importlib.util.spec_from_file_location(
    "evaluator_benchmark", _BENCH / "run_benchmark.py"
)
bench = importlib.util.module_from_spec(_spec)
# Register before exec: @dataclass in the module introspects sys.modules by name.
sys.modules[_spec.name] = bench
_spec.loader.exec_module(bench)


def test_grounding_core_is_perfect_on_design_target() -> None:
    """On clean text, absent-figure hallucination, rounding, and formatting, the
    grounding check must catch every hallucination with no false alarms."""
    g = bench.run_grounding()
    core = g["core"]
    assert core["recall"] == 1.0, core
    assert core["precision"] == 1.0, core
    # No false negatives or false positives on the core subset.
    assert core["fp"] == 0 and core["fn"] == 0, core


def test_grounding_documented_failure_modes_are_present() -> None:
    """The known limitation categories must behave as documented — this keeps the
    benchmark honest (it is measuring real weaknesses, not hiding them)."""
    by_cat = bench.run_grounding()["by_category"]
    # Derived figures are over-flagged (false positives).
    assert by_cat["derived_figure"]["fp"] > 0
    # Mis-attributed figures are missed (false negatives).
    assert by_cat["misattribution"]["fn"] > 0


def test_bias_classification_is_exact() -> None:
    b = bench.run_bias()["overall"]
    assert b["accuracy"] == 1.0, b
    assert b["fp"] == 0 and b["fn"] == 0, b


def test_benchmark_regression_guard_passes() -> None:
    """The script's own floor check (THRESHOLDS) must pass -> main() returns 0."""
    assert bench.main.__module__  # sanity: module loaded
    grounding = bench.run_grounding()
    bias = bench.run_bias()
    summary = {
        "grounding_core_precision": grounding["core"]["precision"],
        "grounding_core_recall": grounding["core"]["recall"],
        "bias_accuracy": bias["overall"]["accuracy"],
    }
    for key, floor in bench.THRESHOLDS.items():
        assert summary[key] >= floor, (key, summary[key], floor)
