# ForkMark evaluator accuracy report

Measured accuracy of ForkMark's own auto-evaluators against hand-labelled ground truth. Regenerate with `python benchmarks/evaluator_accuracy/run_benchmark.py --write-report`.

## Numerical-fidelity (grounding) evaluator

Task: flag model output figures that are not supported by a source document. Positive class = the evaluator flags the output as unfaithful.

**On its design target** (clean text, absent-figure hallucination, rounding, and formatting variants):

- Precision **1.00**, recall **1.00**, F1 **1.00** (n=27).

**On the full set, including its two documented limitation categories:**

- Precision **0.67**, recall **0.71**, F1 **0.69**, accuracy **0.75** (n=36).

| Category | n | Correct | Behaviour |
|---|---|---|---|
| faithful | 10 | 10/10 | true negatives (no false alarms) |
| hallucinated | 10 | 10/10 | true positives (caught) |
| derived_figure | 5 | 0/5 | **false positives** — flags correct derivations |
| misattribution | 4 | 0/4 | **false negatives** — misses mis-attributed figures |
| rounding_tolerance | 3 | 3/3 | true negatives (tolerance/magnitude parsing) |
| formatting | 4 | 4/4 | true negatives (currency/bps/ratio parsing) |

Interpretation: the evaluator is a reconciliation check, and it catches every absent-figure hallucination in the set with no false alarms on clean, rounded, or reformatted text. Its two known limits are visible and quantified: it **over-flags** figures the model legitimately *derives* (sums, ratios, growth rates not present verbatim), and it **cannot catch** a figure that is present in the source but *mis-attributed*. Both are surfaced to a human reviewer as 'review this figure', not asserted as fact — and the roadmap item is a derivation-aware pass to cut the false positives.

## Bias / disparity evaluator

Task: compute the max/min disparity ratio across per-group scores and flag when it exceeds the threshold. Positive class = the evaluator flags disparity.

- Classification accuracy **1.00** over 14 cases (tp 7, tn 7, fp 0, fn 0).

The disparity arithmetic is exact, so accuracy on well-formed inputs is expected to be perfect; the benchmark's value is confirming correct behaviour at the threshold boundary (a ratio of exactly 1.20 passes; 1.21 fails) and on degenerate inputs — a zero-scoring group yields an infinite ratio and is correctly flagged as total exclusion, while all-zero scores are treated as parity. What the benchmark does **not** claim is that the default 1.2 threshold is correct for every regime: that is a calibration choice the deploying institution must make against its own fairness policy.

## Cross-model note

Both evaluators operate purely on model *outputs* (text and numeric scores), never on model internals, so their behaviour is identical regardless of which model produced the output — Jais, Falcon, a GPT-class model, or a fine-tuned in-house model. Supporting a new model is a connector concern, not an evaluation-method concern; these accuracy figures carry across model families unchanged.
