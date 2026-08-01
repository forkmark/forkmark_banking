# ForkMark

**Self-hosted LLM model risk management and validation for regulated financial institutions.**

ForkMark makes structured, defensible model validation the core primitive of your LLM governance process. Run two model configurations side-by-side, quantify the difference with production-grade statistics, capture independent human review, track each model against the regulations that apply to it, and generate a validation memorandum — all on your own infrastructure.

## Why ForkMark?

- **Model inventory** — a risk-tiered system of record for every governed model, with owner, applicable frameworks, and revalidation schedule.
- **Statistical rigour** — win rate with Wilson confidence intervals, Welch's t-test, Cohen's d, Benjamini-Hochberg multiple-comparison control, and power / minimum-detectable-effect analysis.
- **Finance evaluators** — numerical fidelity (fabricated-figure detection), bias/disparity across demographic groups, and output consistency.
- **Human review** — structured decisions with choice, confidence, and rationale, retained as validation evidence.
- **Regulatory mapping** — SR 11-7, EU AI Act, PRA SS1/23, and CBUAE requirements encoded and tracked as artifact coverage.
- **Validation memos** — a 9-section model validation memorandum, exportable as JSON or `.docx`.

## Quick start

```bash
git clone https://github.com/forkmark/forkmark.git
cd forkmark
echo "FM_BOOTSTRAP_TOKEN=$(openssl rand -hex 16)" > .env
docker compose up --build -d
```

Mint your first API key with the bootstrap token, then open `http://localhost:7700` — see the [self-hosted deployment guide](deployment/self-hosted.md) for details. You'll land on the **Compliance Dashboard**; add a model under **Model Inventory** to begin.

## Architecture overview

```
Model Inventory (risk tier, frameworks, validation dates)
    → EvalRun → WorkflowRun → Branch (A/B) → StepOutput → Comparison → Decision
    → Statistical analysis · Finance evaluators · Human review
    → Regulatory coverage → Validation memo (JSON / .docx)
```

Comparisons get automatic divergence scores; decisions capture structured human review with confidence and rationale — the evidence that feeds a validation memorandum.

## Next steps

- [Quickstart guide](getting-started/quickstart.md) — up and running in 5 minutes
- [SDK overview](sdk/overview.md) — instrument your workflows
- [API reference](api/endpoints.md) — full endpoint documentation
- [Deployment guide](deployment/self-hosted.md) — production setup
