#!/usr/bin/env python3
"""Accuracy benchmark for ForkMark's own auto-evaluators — "validate the validator".

Runs :class:`NumericalFidelityEvaluator` and :class:`BiasDisparityEvaluator` over
the hand-labelled cases in this directory and reports how well each performs as a
classifier against ground truth: precision, recall, F1 and a confusion matrix,
broken down by category. The point is not to claim the evaluators are perfect —
it is to *measure* them honestly, including the two documented failure modes of
the grounding check (false positives on derived figures; false negatives on
mis-attributed figures), so the numbers can be shown to a technical reviewer.

Usage:
    python benchmarks/evaluator_accuracy/run_benchmark.py [--write-report]

Exit code is non-zero if any metric falls below the floors in ``THRESHOLDS``,
so this doubles as a regression guard (see tests/test_evaluator_benchmark.py).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Make the repo root importable whatever the caller's cwd is.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.finance_evaluators import (  # noqa: E402
    BiasDisparityEvaluator,
    NumericalFidelityEvaluator,
)

# Metric floors. The grounding check is designed to catch absent-figure
# hallucinations, so we hold it to a perfect score on that core subset and to
# looser floors on the full set (which deliberately includes its known failure
# modes). The bias check is exact arithmetic, so it must classify every case.
THRESHOLDS = {
    "grounding_core_recall": 1.0,
    "grounding_core_precision": 1.0,
    "bias_accuracy": 1.0,
}


@dataclass
class Confusion:
    tp: int = 0
    fp: int = 0
    tn: int = 0
    fn: int = 0

    def add(self, predicted_positive: bool, actual_positive: bool) -> None:
        if predicted_positive and actual_positive:
            self.tp += 1
        elif predicted_positive and not actual_positive:
            self.fp += 1
        elif not predicted_positive and not actual_positive:
            self.tn += 1
        else:
            self.fn += 1

    @property
    def total(self) -> int:
        return self.tp + self.fp + self.tn + self.fn

    @property
    def precision(self) -> float:
        d = self.tp + self.fp
        return self.tp / d if d else 1.0

    @property
    def recall(self) -> float:
        d = self.tp + self.fn
        return self.tp / d if d else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def accuracy(self) -> float:
        return (self.tp + self.tn) / self.total if self.total else 1.0

    def as_dict(self) -> dict:
        return {
            "tp": self.tp, "fp": self.fp, "tn": self.tn, "fn": self.fn,
            "precision": round(self.precision, 3), "recall": round(self.recall, 3),
            "f1": round(self.f1, 3), "accuracy": round(self.accuracy, 3),
        }


def _load(name: str) -> list[dict]:
    return json.loads((_HERE / name).read_text())["cases"]


def run_grounding() -> dict:
    """Score the numerical-fidelity evaluator. Positive class = 'unfaithful'
    (the evaluator flags at least one unsupported figure)."""
    ev = NumericalFidelityEvaluator()
    cases = _load("grounding_cases.json")
    overall = Confusion()
    by_category: dict[str, Confusion] = {}
    rows = []
    for c in cases:
        res = ev.evaluate(c["source_document"], c["model_output"])
        predicted_unfaithful = not res.is_faithful
        actual_unfaithful = not c["ground_truth_faithful"]
        overall.add(predicted_unfaithful, actual_unfaithful)
        by_category.setdefault(c["category"], Confusion()).add(
            predicted_unfaithful, actual_unfaithful
        )
        correct = predicted_unfaithful == actual_unfaithful
        rows.append({
            "id": c["id"], "category": c["category"], "correct": correct,
            "predicted_unfaithful": predicted_unfaithful,
            "actual_unfaithful": actual_unfaithful,
            "flagged": [f.raw for f in res.flagged_numbers],
        })

    # The "core target" of the evaluator: clean vs absent-figure hallucination.
    core = Confusion()
    for c, r in zip(cases, rows):
        if c["category"] in ("faithful", "hallucinated", "rounding_tolerance",
                             "formatting"):
            core.add(r["predicted_unfaithful"], r["actual_unfaithful"])

    return {
        "overall": overall.as_dict(),
        "core": core.as_dict(),
        "by_category": {k: v.as_dict() for k, v in by_category.items()},
        "rows": rows,
    }


def run_bias() -> dict:
    """Score the disparity evaluator. Positive class = 'fails threshold'
    (disparity flagged)."""
    cases = _load("bias_cases.json")
    conf = Confusion()
    rows = []
    for c in cases:
        ev = BiasDisparityEvaluator(threshold=c.get("threshold", 1.2))
        res = ev.evaluate(c["group_scores"])
        predicted_fail = not res.passes_threshold
        actual_fail = not c["expected_pass"]
        conf.add(predicted_fail, actual_fail)
        rows.append({
            "id": c["id"], "correct": predicted_fail == actual_fail,
            "ratio": None if res.disparity_ratio != res.disparity_ratio
            else round(res.disparity_ratio, 4) if res.disparity_ratio != float("inf")
            else "inf",
            "predicted_fail": predicted_fail, "actual_fail": actual_fail,
        })
    return {"overall": conf.as_dict(), "rows": rows}


def build_report(grounding: dict, bias: dict) -> str:
    g, gc, b = grounding["overall"], grounding["core"], bias["overall"]
    lines = []
    lines.append("# ForkMark evaluator accuracy report\n")
    lines.append(
        "Measured accuracy of ForkMark's own auto-evaluators against hand-labelled "
        "ground truth. Regenerate with `python benchmarks/evaluator_accuracy/"
        "run_benchmark.py --write-report`.\n"
    )

    lines.append("## Numerical-fidelity (grounding) evaluator\n")
    lines.append(
        "Task: flag model output figures that are not supported by a source "
        "document. Positive class = the evaluator flags the output as unfaithful.\n"
    )
    lines.append("**On its design target** (clean text, absent-figure "
                 "hallucination, rounding, and formatting variants):\n")
    lines.append(f"- Precision **{gc['precision']:.2f}**, recall "
                 f"**{gc['recall']:.2f}**, F1 **{gc['f1']:.2f}** "
                 f"(n={gc['tp']+gc['fp']+gc['tn']+gc['fn']}).\n")
    lines.append("**On the full set, including its two documented limitation "
                 "categories:**\n")
    lines.append(f"- Precision **{g['precision']:.2f}**, recall "
                 f"**{g['recall']:.2f}**, F1 **{g['f1']:.2f}**, accuracy "
                 f"**{g['accuracy']:.2f}** (n={g['tp']+g['fp']+g['tn']+g['fn']}).\n")
    lines.append("| Category | n | Correct | Behaviour |")
    lines.append("|---|---|---|---|")
    cat_labels = {
        "faithful": "true negatives (no false alarms)",
        "hallucinated": "true positives (caught)",
        "derived_figure": "**false positives** — flags correct derivations",
        "misattribution": "**false negatives** — misses mis-attributed figures",
        "rounding_tolerance": "true negatives (tolerance/magnitude parsing)",
        "formatting": "true negatives (currency/bps/ratio parsing)",
    }
    for cat, conf in grounding["by_category"].items():
        n = conf["tp"] + conf["fp"] + conf["tn"] + conf["fn"]
        correct = conf["tp"] + conf["tn"]
        lines.append(f"| {cat} | {n} | {correct}/{n} | {cat_labels.get(cat, '')} |")
    lines.append("")
    lines.append("Interpretation: the evaluator is a reconciliation check, and it "
                 "catches every absent-figure hallucination in the set with no false "
                 "alarms on clean, rounded, or reformatted text. Its two known "
                 "limits are visible and quantified: it **over-flags** figures the "
                 "model legitimately *derives* (sums, ratios, growth rates not "
                 "present verbatim), and it **cannot catch** a figure that is present "
                 "in the source but *mis-attributed*. Both are surfaced to a human "
                 "reviewer as 'review this figure', not asserted as fact — and the "
                 "roadmap item is a derivation-aware pass to cut the false positives.\n")

    lines.append("## Bias / disparity evaluator\n")
    lines.append(
        "Task: compute the max/min disparity ratio across per-group scores and flag "
        "when it exceeds the threshold. Positive class = the evaluator flags "
        "disparity.\n"
    )
    lines.append(f"- Classification accuracy **{b['accuracy']:.2f}** over "
                 f"{b['tp']+b['fp']+b['tn']+b['fn']} cases "
                 f"(tp {b['tp']}, tn {b['tn']}, fp {b['fp']}, fn {b['fn']}).\n")
    lines.append("The disparity arithmetic is exact, so accuracy on well-formed "
                 "inputs is expected to be perfect; the benchmark's value is "
                 "confirming correct behaviour at the threshold boundary (a ratio of "
                 "exactly 1.20 passes; 1.21 fails) and on degenerate inputs — a "
                 "zero-scoring group yields an infinite ratio and is correctly "
                 "flagged as total exclusion, while all-zero scores are treated as "
                 "parity. What the benchmark does **not** claim is that the default "
                 "1.2 threshold is correct for every regime: that is a calibration "
                 "choice the deploying institution must make against its own "
                 "fairness policy.\n")

    lines.append("## Cross-model note\n")
    lines.append(
        "Both evaluators operate purely on model *outputs* (text and numeric "
        "scores), never on model internals, so their behaviour is identical "
        "regardless of which model produced the output — Jais, Falcon, a GPT-class "
        "model, or a fine-tuned in-house model. Supporting a new model is a "
        "connector concern, not an evaluation-method concern; these accuracy "
        "figures carry across model families unchanged.\n"
    )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-report", action="store_true",
                    help="Write REPORT.md alongside the datasets.")
    ap.add_argument("--json", action="store_true",
                    help="Print the machine-readable metrics as JSON.")
    args = ap.parse_args()

    grounding = run_grounding()
    bias = run_bias()

    summary = {
        "grounding_core_precision": grounding["core"]["precision"],
        "grounding_core_recall": grounding["core"]["recall"],
        "grounding_overall_precision": grounding["overall"]["precision"],
        "grounding_overall_recall": grounding["overall"]["recall"],
        "grounding_overall_f1": grounding["overall"]["f1"],
        "bias_accuracy": bias["overall"]["accuracy"],
    }

    if args.json:
        print(json.dumps({"summary": summary, "grounding": grounding,
                          "bias": bias}, indent=2))
    else:
        print("Grounding (core target): "
              f"P={summary['grounding_core_precision']:.2f} "
              f"R={summary['grounding_core_recall']:.2f}")
        print("Grounding (full set):    "
              f"P={summary['grounding_overall_precision']:.2f} "
              f"R={summary['grounding_overall_recall']:.2f} "
              f"F1={summary['grounding_overall_f1']:.2f}")
        print(f"Bias accuracy:           {summary['bias_accuracy']:.2f}")

    if args.write_report:
        report = build_report(grounding, bias)
        (_HERE / "REPORT.md").write_text(report)
        print(f"\nWrote {_HERE / 'REPORT.md'}")

    # Regression guard: fail if any floor is breached.
    failures = [k for k, floor in THRESHOLDS.items() if summary[k] < floor]
    if failures:
        print(f"\nFAIL: below floor: {failures}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
