# ForkMark

**ForkMark is a self-hosted validation platform for the AI and LLM models that regulated financial institutions deploy — the AI-specific evidence layer for the model risks your existing MRM framework doesn't cover.**

Validate AI/LLM models against the regulations that bind you — the UAE's CBUAE regimes first, with the EU AI Act, US SR 26-2, and UK PRA as options — with defensible statistics, bias and hallucination testing, independent human review, and regulator-ready validation memoranda. All on your own infrastructure, with no data leaving your network — the deployment model UAE banking-data-localization rules require.

[![CI](https://github.com/forkmark/forkmark_banking/actions/workflows/ci.yml/badge.svg)](https://github.com/forkmark/forkmark_banking/actions)

---

## Why ForkMark

Model risk teams at banks and other regulated firms are now accountable for the large language models their institutions deploy — but most LLM tooling is built for experimentation, not supervision. ForkMark treats an LLM like any other model under management: it must be inventoried, risk-tiered, validated on a schedule, tested for bias, kept under human oversight, and documented well enough to satisfy a supervisor.

ForkMark provides the evaluation, statistics, evidence capture, and reporting to do that, mapped explicitly to the frameworks that govern model risk in financial services.

## Regulatory context

ForkMark is **UAE-first**. It is designed around six model risk management / AI governance regimes — the three UAE regimes lead, with the EU, US, and UK offered as additional coverage. The requirement metadata is encoded in the platform (`core/regulatory_frameworks.py`) as a set of required evidence artifacts per framework, and drives coverage tracking and validation memos. Mapping is at the requirement/artifact level rather than clause-by-clause. This is engineering guidance, not legal advice.

| Framework | Jurisdiction | Focus |
|---|---|---|
| **CBUAE MMS** | 🇦🇪 UAE (Central Bank of the UAE) | The binding **Model Management Standards & Guidance (2022)** — the lifecycle model-risk regime every UAE bank is examined against: model inventory, materiality tiering, development, independent validation, ongoing monitoring, governance, and data management. |
| **CBUAE AI Guidance** | 🇦🇪 UAE (Central Bank of the UAE) | The **Guidance Note on Artificial Intelligence and Machine Learning (11 February 2026)**, read with the binding MMS. It brings AI systems inside the model-risk perimeter, and sets five principles: governance, fairness, transparency, human oversight, and data privacy — including consumer disclosure and explainability in **Arabic and English**, with accountability retained even for third-party/cloud models. This is ForkMark's lead framework. |
| **UAE Joint Guidelines** | 🇦🇪 UAE (CBUAE · SCA · DFSA/DIFC · FSRA/ADGM) | *Guidelines for Financial Institutions Adopting Enabling Technologies* (2021), issued jointly across the UAE's financial regulators: AI governance, model validation, material-application registries with version control, and vendor due diligence — one mapping across mainland, DIFC, and ADGM. |
| **EU AI Act** | 🇪🇺 European Union | Obligations for high-risk AI systems: risk management, data governance, technical documentation, record-keeping, transparency, human oversight, accuracy/robustness, mandatory bias testing, and CE marking. |
| **SR 26-2** | 🇺🇸 US (Fed / OCC / FDIC) | The current US model-risk standard (interagency, 17 April 2026), **superseding SR 11-7** — risk-based and materiality-sensitive. Notably it **excludes generative and agentic AI** as "novel and rapidly evolving", leaving the AI-model governance gap ForkMark is built to fill. Unlike the CBUAE Standards it is **guidance rather than a binding rule**. |
| **PRA SS1/23** | 🇬🇧 UK (Bank of England / PRA) | Technology-agnostic, outcomes-focused MRM principles (in force since 2024): model identification and risk tiering, governance, development, independent validation, and risk mitigants. |

**On the EU AI Act timeline.** The AI Act entered into force on 1 August 2024. Obligations for Annex III (use-case) high-risk systems were originally scheduled to apply from 2 August 2026; under the 2026 *Digital Omnibus* simplification package they were deferred to 2 December 2027, and high-risk AI embedded in regulated products under Annex I to 2 August 2028. The Article 50 transparency duties and the Article 4 AI-literacy duty were **not** deferred and remain due from 2 August 2026. Firms should confirm the current applicability date for their systems — ForkMark helps you build the conformity evidence (technical documentation, bias testing, human-oversight records) these obligations require, whatever the effective date.

## Who this is for

ForkMark is built for the people accountable for model risk in regulated financial services:

- **Model risk management teams** who must inventory, tier, validate, and revalidate models on a defensible cadence.
- **AI governance officers** who need auditable evidence that models are fair, monitored, and under human oversight.
- **Quant and validation analysts** at banks, fintechs, and other regulated financial-services firms who perform independent challenge and write validation memos.

## Who this is NOT for

ForkMark is deliberately narrow. It is **not** a good fit for:

- **General ML experimentation** or research tooling — use an experiment tracker or notebook platform.
- **Fine-tuning / preference-optimization workflows** — ForkMark validates and governs models; it does not train them and does not export training corpora.
- **Non-regulated industries** with no model risk or AI-governance obligations, where its compliance scaffolding is overhead rather than value.

## Quick start (Docker)

```bash
git clone https://github.com/forkmark/forkmark_banking.git
cd forkmark_banking

# 1) Set a bootstrap token used to mint your first API key.
echo "FM_BOOTSTRAP_TOKEN=$(openssl rand -hex 16)" > .env

# 2) Build and start the stack.
docker compose up --build -d
```

ForkMark is secure-by-default: an API key is required for every UI endpoint (this is mandatory for financial-institution deployments). Mint the first key with your bootstrap token:

```bash
source .env
curl -X POST http://localhost:7700/api/keys \
     -H "Content-Type: application/json" \
     -H "X-API-Key: $FM_BOOTSTRAP_TOKEN" \
     -d '{"name":"admin"}'
# → {"id": "...", "name": "admin", "raw_key": "fm_..."}
```

Open **http://localhost:7700**, go to **API Keys / Settings**, and paste the returned `raw_key`. You'll land on the **Compliance Dashboard**. Add your first model under **Model Inventory** to begin tracking coverage and revalidation.

> Prefer no container? `pip install -r requirements.txt && python run.py` runs the same app on `http://localhost:7700` (loopback). Set `FM_REQUIRE_UI_AUTH=false` only for isolated local development with no real model data.

## Capabilities mapped to requirements

| ForkMark capability | What it produces | Supports |
|---|---|---|
| **Model inventory** (`/inventory`) | Risk-tiered system of record with owner, frameworks, and validation dates | CBUAE MMS & AI Guidance model inventory; SR 26-2 inventory; PRA SS1/23 Principle 1 (identification & tiering) |
| **Statistical analysis** (`/statistics`) | Win rate + Wilson CI, **paired t-test with paired Cohen's d_z** (default), Welch's independent-samples t-test with pooled *d* (opt-in), Benjamini-Hochberg FDR, power/MDE | SR 26-2 & PRA SS1/23 outcomes analysis; EU AI Act accuracy evidence |
| **Champion vs. challenger** | The live model retained as the baseline and re-tested against a proposed version | CBUAE re-testing on every model upgrade; ongoing monitoring |
| **Bias & fairness evaluator** | Cross-group disparity ratio with a configurable threshold | EU AI Act bias testing; CBUAE fairness principle |
| **Numerical fidelity evaluator** | Flags fabricated/altered figures vs. a source document | Accuracy / robustness; guards against material misstatement |
| **Consistency evaluator** | Output stability (coefficient of variation) across paraphrases | Robustness of compliance-critical decisions |
| **Human review capture** | Structured decisions with confidence and rationale | SR 26-2 effective challenge; EU AI Act / CBUAE human oversight |
| **Bilingual disclosure** | Validation memoranda with an **Arabic and English** executive summary, generated in-app | CBUAE transparency/explainability expectation for AI-assisted decisions |
| **Regulatory coverage** (`/regulatory`) | Present vs. missing evidence artifacts per framework | Documentation completeness across all six regimes |
| **Access control & audit** (RBAC + `/audit/log`) | Role-scoped API keys (viewer/reviewer/admin) and a hash-chained, tamper-evident audit trail of supervisory mutations, verifiable via `/audit/verify` | Segregation of duties and auditability (SR 26-2 governance; CBUAE MMS) |
| **Validation memos** (`/compliance`) | 9-section validation memorandum as JSON or `.docx` | The written validation record a supervisor expects |
| **Revalidation calendar** | Models due for revalidation within a window | Ongoing monitoring and periodic revalidation |

## What is built, and what is not

The CBUAE's February 2026 AI guidance sets eight testable obligations for licensed institutions. Six are addressed by capabilities that compute today. Two are not built yet, and are listed here rather than implied by omission.

| Obligation under the Feb 2026 guidance | ForkMark | Status |
|---|---|---|
| Inventory of every AI system, with a risk rating | Model inventory with risk tiers, linked to each validation run | **Built** |
| Bias tested periodically and on every upgrade | Group-disparity evaluator; champion-vs-challenger re-run per version | **Built** |
| No unsupported or ungrounded outputs | Numerical fidelity evaluator against a source document | **Built** |
| Meaningful human oversight and challenge | Human review capture with rationale and attribution | **Built** |
| Data provenance and audit trails | Hash-chained, tamper-evident audit log (verifiable via `/audit/verify`) with role-based access control | **Built** |
| Disclosure in Arabic and English | Bilingual executive summary in the generated memorandum | **Built** |
| Stress-testing and continuous monitoring | Safety / jailbreak suite and drift monitoring | *Not built* |
| Vendor AI held to the in-house standard | Native connectors for vendor and sovereign models (Jais, Falcon, GPT) | *Not built* |

Vendor models can still be evaluated today by streaming their outputs in through the SDK; what is missing is first-class connectors. See `examples/jais_banking_demo/` and `examples/adverse_action_demo/` for a worked end-to-end validation on an Arabic retail-banking assistant.

## SDK integration

Already have outputs from your own pipeline? Log a comparison in a few lines with the Python SDK:

```python
import forkmark

forkmark.init(api_key="fm_...", base_url="http://localhost:7700")

with forkmark.run("credit-memo-summary", input_data={"clause": clause}) as run:
    run.log_step_output("summarise",
                        messages=[{"role": "user", "content": clause}],
                        output=response_a, model="gpt-4o",            branch="A")
    run.log_step_output("summarise",
                        messages=[{"role": "user", "content": clause}],
                        output=response_b, model="claude-3.5-sonnet", branch="B")
```

ForkMark scores the divergence and creates the comparison automatically. Review it, record a decision, and it becomes human-review evidence in a validation memo.

## Architecture

ForkMark is a single Python process (FastAPI) that serves both the REST API and the React frontend. SQLite by default; PostgreSQL for production via `FM_DATABASE_URL`. No external services are required.

```
Browser ──▶ FastAPI (port 7700) ──▶ SQLite / PostgreSQL
                 │
                 ├── React SPA (served from /frontend/dist)
                 ├── Divergence scoring + finance evaluators
                 └── Statistics · compliance reporting (.docx)
```

Full REST API with OpenAPI docs at **`/docs`** (all regulatory, inventory, statistics, and compliance routes are documented there).

### Editions — what ships today vs. roadmap

To avoid confusion between what runs now and what is planned, ForkMark is explicit about scope:

**Ships today (open-source edition):** the single-process FastAPI app on SQLite/PostgreSQL; the full model-risk core (inventory, statistics, evaluators, regulatory coverage for all six regimes, bilingual validation memos); secure-by-default API-key auth **with role-based access control** (viewer/reviewer/admin) and a **hash-chained, tamper-evident audit log** (`/api/audit/log`, verifiable via `/api/audit/verify`); in-process background scoring; and optional Redis-backed caching/rate-limiting.

**Enterprise roadmap (opt-in via `FM_ENTERPRISE_MODE`, in the `ee/` package):** multi-tenant workspace isolation, SCIM/SSO provisioning, device-flow auth, data-residency routing, a Redis message bus, Celery workers, and OpenTelemetry observability. These load on a best-effort basis and degrade gracefully when their optional services (PostgreSQL, Redis, WorkOS) are absent; the platform guide's scaling sections describe this target architecture, not the default self-host.

## Configuration

All settings are environment variables; see [`.env.example`](.env.example) for the full list.

| Variable | Default | Description |
|---|---|---|
| `FM_PORT` | `7700` | Server port |
| `FM_DATABASE_URL` | (SQLite) | PostgreSQL URL for production |
| `FM_REQUIRE_UI_AUTH` | `true` | Require an API key for all UI endpoints. **Keep enabled** in any regulated deployment. |
| `FM_BOOTSTRAP_TOKEN` | | One-time token to mint the first API key when bound off-loopback |
| `FM_SECRET_KEY` | | Enables Fernet encryption-at-rest for stored provider API keys |
| `FM_OPENAI_API_KEY` | | Required for LLM-judge scoring and the playground |

> **Security note.** ForkMark stores supervisory records — model validation evidence, human-review decisions, statistical results, and the model inventory. `FM_REQUIRE_UI_AUTH` defaults to `true` so these are never served unauthenticated, even on loopback. Set `FM_SECRET_KEY` to encrypt stored provider keys at rest.

## Development

```bash
# Backend
pip install -r requirements.txt
python run.py

# Frontend (hot reload)
cd frontend && npm install && npm run dev

# Tests
pytest tests/
```

## License

MIT
