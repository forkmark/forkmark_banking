# Why ForkMark

Most LLM evaluation tools start from logging and tack on evaluation as an afterthought. ForkMark inverts this: **structured, validated comparison is the core primitive**, and everything else — statistics, human review, and compliance evidence — flows from it.

## The problem with current approaches

LLM teams iterate fast. You change a prompt, swap a model, tweak retrieval parameters, and redeploy. But how do you know if the change actually made things better?

Common approaches have real limitations:

**Vibes-based evaluation** — You eyeball a few outputs, decide it "feels better," and ship. This doesn't scale, isn't reproducible, and can't catch regressions across edge cases.

**Automated metrics** — BLEU, ROUGE, and other reference-based metrics are fast but poorly correlated with human preference for open-ended generation. You optimize for a number that doesn't reflect what users actually care about.

**LLM-as-judge** — Sending outputs to GPT-4 for scoring is convenient but introduces its own biases (position bias, verbosity bias, self-preference). Without human ground truth, you're building on sand.

**Logging platforms with eval bolted on** — Tools like LangSmith, Braintrust, and Weights & Biases start from observability and add evaluation as a feature. The evaluation UX is secondary to the logging story, and the data model wasn't designed for structured preference collection.

## How ForkMark is different

### Comparison-first data model

Every evaluation in ForkMark is a pairwise comparison. You define two branch configurations (different models, prompts, or parameters), run them against the same inputs, and get structured side-by-side outputs.

This isn't just a UI choice — it's a data model decision. Every `Comparison` entity pairs two `StepOutput` records and captures automatic divergence scores. Every `Decision` records a structured human verdict with choice, confidence, rationale for selection, and rationale for rejection.

### Position debiasing built in

LLM judges and human reviewers both exhibit position bias — they tend to prefer whichever output appears first. ForkMark randomizes presentation order at the comparison level, ensuring your evaluation data isn't systematically skewed.

### Four-tier divergence scoring

Not every comparison needs human review. ForkMark's automatic scoring pipeline identifies which outputs actually differ:

- **Lexical** — edit distance for surface-level changes
- **Semantic** — sentence-transformer similarity for paraphrase detection
- **Embedding** — OpenAI embedding cosine distance for deeper semantic comparison
- **LLM-as-judge** — model-graded quality assessment with configurable rubrics

Reviewers focus their time on comparisons that actually diverge, rather than confirming that two identical outputs are identical.

### Validation memos as a first-class output

Here's the key insight: if you're already doing structured A/B evaluation with independent human review, you're generating exactly what a model validator needs — win rates with confidence intervals, significance tests, bias and fidelity checks, and documented human oversight.

ForkMark makes this explicit. Every decision you record becomes audit evidence, and the platform assembles it — alongside the statistics and evaluator results — into a nine-section model validation memorandum, exportable as JSON or `.docx` and mapped to SR 11-7, the EU AI Act, PRA SS1/23, or CBUAE requirements.

### Consent-gated data collection

Exporting human review data can raise governance questions. ForkMark's consent framework lets reviewers explicitly opt in or out of having their decisions included in exports. Consent is tracked per-reviewer, per-workflow, and can be revoked at any time.

### Self-hosted by design

Your prompts, outputs, and evaluation data are sensitive. ForkMark runs entirely on your infrastructure — there are no external API calls except to the LLM providers you explicitly configure.

The default setup is a single Python process with SQLite, deployable on a laptop in 30 seconds. Scale to PostgreSQL and multiple workers when you need it.

## Who is ForkMark for?

**Model risk management teams** who must inventory, tier, validate, and revalidate the LLMs their institution deploys on a defensible cadence.

**AI governance officers** who need auditable evidence that models are fair, monitored, and under human oversight.

**Quant and validation analysts** performing independent challenge and writing validation memoranda at banks, fintechs, and other regulated financial-services firms.

## What ForkMark is not

ForkMark is not a logging or observability platform. It doesn't instrument your production traffic or provide real-time dashboards of latency and token usage. Use your existing observability stack for that.

ForkMark is not an auto-eval framework. It doesn't replace the need for human judgment — it structures and amplifies it.

ForkMark is not a model hosting platform. It calls your existing LLM endpoints and evaluates the outputs.

## Getting started

```bash
git clone https://github.com/forkmark/forkmark.git
cd forkmark
python run.py
```

Open `http://localhost:7700`, try the Demo Gallery, and run your first evaluation in under five minutes. See the [quickstart guide](getting-started/quickstart.md) for a full walkthrough.
