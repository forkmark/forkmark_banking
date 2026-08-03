# Evaluator accuracy benchmark — "validate the validator"

ForkMark's job is to validate a bank's AI models, so the obvious question a
model-risk reviewer (or a technical investor) asks is: **how accurate are
ForkMark's own auto-evaluators?** This benchmark answers that with measured
numbers rather than assertion.

## What it measures

Two of the live evaluators are auto-scored against hand-labelled ground truth:

- **Numerical-fidelity (grounding)** — `NumericalFidelityEvaluator`. Given a
  source document and a model output, does it correctly identify outputs that
  contain a figure not supported by the source? Scored as a binary classifier
  (positive = flagged as unfaithful): precision, recall, F1, and a per-category
  confusion matrix.
- **Bias / disparity** — `BiasDisparityEvaluator`. Given per-group scores, does
  it correctly flag disparity above the threshold? Scored for classification
  accuracy, boundary behaviour, and degenerate inputs.

The datasets deliberately include categories that probe the grounding check's
**documented limitations** (see the evaluator docstring): `derived_figure` cases
(the model computes a correct sum/ratio not present verbatim — a false-positive
mode) and `misattribution` cases (a figure is present in the source but attached
to the wrong entity — a false-negative mode). Reporting these honestly is the
point: the benchmark shows both where the evaluator is reliable and where it is
not.

## Files

| File | Purpose |
|---|---|
| `grounding_cases.json` | Labelled source/output pairs for the grounding check |
| `bias_cases.json` | Labelled per-group score sets for the disparity check |
| `run_benchmark.py` | Runs the evaluators, computes metrics, writes `REPORT.md` |
| `REPORT.md` | The generated results (regenerate any time) |

## Running it

```bash
python benchmarks/evaluator_accuracy/run_benchmark.py            # print summary
python benchmarks/evaluator_accuracy/run_benchmark.py --write-report
python benchmarks/evaluator_accuracy/run_benchmark.py --json     # full metrics
```

The script exits non-zero if any metric falls below the floors in `THRESHOLDS`
(perfect precision/recall on the grounding *design target*; exact bias
classification), so it doubles as a regression guard. `tests/test_evaluator_
benchmark.py` runs it in CI.

## Honest scope

This is a small, curated benchmark (tens of cases), not a large-sample
statistical evaluation. It is enough to characterise the evaluators' behaviour
and pin their documented failure modes with concrete numbers; scaling it to a
larger, independently-labelled corpus — and adding a derivation-aware grounding
pass to cut the false positives — is tracked roadmap work.
