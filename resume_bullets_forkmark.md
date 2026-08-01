# ForkMark — Resume Bullets (Applied AI Scientist framing)

**Founder & Sole Engineer, ForkMark** — Self-hosted LLM evaluation & validation platform for regulated financial institutions
*Jun 2026 – Present*

## Primary set (7 bullets)

- **What it is:** Conceived and built ForkMark, a self-hosted platform that measures whether a language model is safe to deploy in a regulated banking workflow — running champion-vs-challenger comparisons, scoring outputs with programmatic evaluators, testing the results for statistical significance, and compiling the evidence into an auditable validation record.

- **Who it's for:** Designed for the teams accountable for AI in regulated finance — model risk and validation groups who must inventory, tier, and revalidate models on a defensible cadence, and the applied AI/ML teams who need evidence that a model's outputs are accurate, unbiased, and stable before it ships. Deploys entirely on-premise, so no prompts, outputs, or customer data leave the institution's network.

- **Origin & adoption:** Originated the concept from four years validating models in Wells Fargo's Model Risk Management (MRM) team; presented ForkMark internally at Wells Fargo, where it is now in use by the MRM AI validation team for model evaluation and validation evidence.

- **Evaluation core:** Designed a multi-signal output divergence scorer combining TF-IDF lexical cosine, sentence-transformer and OpenAI embedding similarity, and LLM-as-judge grading, with graceful degradation across scorers so evaluation stays reproducible in air-gapped deployments with no external API access.

- **Statistical rigor:** Implemented the inference layer for A/B model comparison — paired and Welch's t-tests, Cohen's d and d_z effect sizes, Wilson score intervals for win rates, Benjamini–Hochberg FDR correction across multiple evaluators, and a priori power analysis / minimum-detectable-effect calculation — replacing eyeballed win rates with conclusions that survive statistical scrutiny.

- **Evaluators:** Built a library of programmatic LLM evaluators, including a numerical-fidelity checker that extracts and reconciles figures against source documents to detect fabricated or altered numbers, a bias evaluator computing cross-group disparity ratios on protected attributes, and a consistency evaluator measuring output stability (coefficient of variation) across paraphrased prompts; exposed through a pluggable registry for custom pointwise and pairwise evaluators. Extended the same machinery to agentic systems via a trajectory comparator scoring tool-call sequences by Levenshtein distance, outcome equivalence, and cost/latency/token efficiency.

- **How it's built:** Architected and wrote the full ~34K-LOC stack solo — FastAPI/Python backend, React SPA, SQLite/Postgres with 13 versioned migrations behind a dialect-portable SQL layer, Docker deployment, role-scoped API-key RBAC with an append-only audit log, and a Python SDK that instruments an existing inference pipeline in three lines. 480+ tests (388 backend, 93 frontend) running in CI across Python 3.10–3.12 and a live Postgres service, including dedicated suites for statistical correctness and access control.

## Optional additional bullets

- Encoded six model-risk and AI-governance regimes (CBUAE Model Management Standards, CBUAE AI Guidance, EU AI Act, US SR 26-2, UK PRA SS1/23, UAE Joint Guidelines) as machine-readable requirement metadata driving automated evidence-coverage tracking.

- Built a human-in-the-loop review layer (structured decisions, confidence, rationale) feeding an evidence pipeline that assembles computed evaluator results and reviewer judgments into a 9-section validation memorandum rendered to JSON/`.docx`, with bilingual Arabic/English RTL output for UAE regulators.

- Sole founder; authored the technical and go-to-market case for the Hub71 (Abu Dhabi) Initiate program and an active pre-seed raise.

## Condensed 4-bullet version (space-constrained resumes)

- Conceived and solo-built ForkMark (~34K LOC; FastAPI, React, Postgres, Docker), a self-hosted platform that evaluates whether an LLM is safe to deploy in a regulated banking workflow — for model risk teams and the applied AI teams whose models they govern, deployed entirely on the institution's own infrastructure with a default scoring path that makes no external API calls.

- Originated the idea from four years in Wells Fargo's Model Risk Management (MRM) team; presented it internally at Wells Fargo, where the MRM AI validation team now uses it for model evaluation.

- Designed the evaluation and inference stack: multi-signal divergence scoring (TF-IDF lexical, sentence-transformer and OpenAI embeddings, LLM-as-judge) with automatic fallback, paired/Welch t-tests with Cohen's d and d_z, Wilson score intervals, Benjamini–Hochberg FDR control, and power/MDE analysis; plus programmatic evaluators for numerical hallucination, cross-group bias disparity, and output consistency.

- Extended evaluation to agent trajectories (tool-sequence Levenshtein distance, outcome equivalence, cost/latency/token efficiency) and built an evidence pipeline that auto-assembles computed evaluator results and human review decisions into auditable validation reports; 480+ tests in CI.

---

## Verification log (checked against source, 19 Jul 2026 — not for the resume)

| Claim | Verdict | Evidence |
|---|---|---|
| Multi-signal divergence: lexical, embedding, LLM-as-judge | ✅ | `core/comparator.py` — TF-IDF cosine, sentence-transformers, OpenAI embeddings API, G-Eval-style judge; documented fallback cascade to lexical |
| Paired/Welch t-tests, effect sizes | ✅ | `paired_t_test`, `welch_t_test`, `cohens_d`, `paired_cohens_d` (d_z); paired is the default in `analyze()` |
| Wilson score intervals | ✅ | `wilson_score_interval`, used for every win-rate CI |
| Benjamini–Hochberg FDR | ✅ | `benjamini_hochberg`, wired into `analyze_batch`; `is_significant` decided on the adjusted p-value |
| Power analysis / MDE | ✅ | `power_analysis`, `minimum_detectable_effect`; MDE returned on every result |
| Numerical hallucination evaluator | ✅ | `NumericalFidelityEvaluator` — extracts figures, reconciles to source at 1bp tolerance, flags unsupported numbers |
| Cross-group bias disparity | ✅ | `BiasDisparityEvaluator` — max/min group ratio vs. configurable threshold (default 1.2) |
| Output consistency | ✅ | `ConsistencyEvaluator` — coefficient of variation across paraphrase scores, threshold 0.15 |
| Tool-sequence Levenshtein | ✅ | `_levenshtein_distance` over extracted tool-call sequences in `tool_sequence_score` |
| Outcome equivalence | ✅ | `outcome_equivalence_score` inverts the divergence scorer over final outputs, `SequenceMatcher` fallback |
| Efficiency | ✅ (reworded) | `efficiency_score` = weighted similarity across cost (.35), latency (.25), tokens (.25), tool count (.15) — a resource profile, not "steps" |
| Evidence pipeline | ✅ | `compliance.py::_build_evidence_from_model` pulls linked eval runs and real human decisions, computes NumericalFidelity over champion-vs-challenger outputs plus BiasDisparity when group signals exist → 9-section memo (JSON/.docx), logged to `compliance_reports` and the audit log |
| ~34K LOC; FastAPI/React/Postgres/Docker | ✅ | 24.6K Python + 9.4K JS/JSX; `Dockerfile`, `docker-compose.yml`, Postgres via `FM_DATABASE_URL` |
| **"400+ tests"** | ❌ **corrected** | Actual: **388 backend** (384 pass, 4 live-Postgres skips) + **93 frontend** = **481**. Now stated as 480+ |
| **"no data egress"** | ⚠️ **softened** | True on the default path, but the `openai` and `llm_judge` scorers send outputs to OpenAI if configured. Now "default scoring path makes no external API calls" |

**Two caveats to be ready for in an interview** (they don't change the bullets):

1. `BiasDisparityEvaluator` and `ConsistencyEvaluator` consume scores you supply — group-level aggregates and paraphrase-set scores respectively. They compute the statistic; they don't infer group membership or generate the paraphrases.
2. The auto-evidence pipeline computes numerical fidelity and bias disparity only. Consistency is not wired into it.

**Unrelated bug spotted while verifying:** `.github/workflows/ci.yml` sets `FM_DB_PATH: ":memory:"`, which gives each SQLite connection its own private database. Under that setting `test_banking_routes.py` and `test_rbac_audit.py` fail with "no such table: model_inventory"; both pass with a file-backed path. Worth checking whether CI is actually green.
