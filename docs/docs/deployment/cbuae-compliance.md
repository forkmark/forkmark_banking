# CBUAE & GCC Model Risk Management (MRM) Compliance Guide

This guide details how ForkMark aligns with the regulatory standards of the **Central Bank of the United Arab Emirates (CBUAE)** under the **Standards for Model Management (MMS 2022)** and the **Guidance Note on the Responsible Adoption and Use of Artificial Intelligence and Machine Learning (11 February 2026)**, alongside alignment with US Fed SR 26-2, EU AI Act, and UK PRA SS1/23.

---

## 1. Regulatory Framework & Alignment

CBUAE model governance operates under two core regulatory instruments:
1. **Standards for Model Management (MMS 2022)**: The mandatory supervisory standard establishing model risk governance across lifecycle sections:
   - **Section 4 (Model Governance & Model Inventory)**: Model governance structure, enterprise-wide model inventory, materiality and risk tiering (Tier 1/2/3 or High/Medium/Low), validation schedules, and ownership accountability.
   - **Section 5 (Data Management)**: Data quality, lineage, relevance, and representative sampling for model development and evaluation.
   - **Section 6 (Model Development)**: Methodological soundness, conceptual justification, and design documentation.
   - **Section 7 (Model Implementation)**: Controlled testing, deployment verification, and operational readiness.
   - **Section 8 (Model Usage)**: Appropriate use within defined business boundaries and ongoing user controls.
   - **Section 9 (Model Monitoring)**: Ongoing tracking of performance metrics, degradation thresholds, and stability.
   - **Section 10 (Independent Validation)**: Rigorous independent challenge, including **Section 10.4 (Quantitative Validation)** for numerical accuracy, benchmark comparison, and sensitivity testing, and **Section 10.6 (Reporting of Findings)** for formal validation reports delivered to the Model Oversight Committee.
2. **Guidance Note on the Responsible Adoption and Use of AI and ML (11 February 2026)**: Comprehensive advisory principles issued to licensed financial institutions (LFIs) structured into sections and lettered sub-clauses:
   - **Section 3(c) (Fairness & Bias Mitigation)**: Testing for algorithmic bias, preventing unfair discrimination, and evaluating demographic parity across customer segments in credit adjudication and financial services.
   - **Section 4(b) (Transparency & Bilingual Consumer Disclosure)**: Plain-language disclosures in both Arabic and English explaining model capabilities, limitations, and automated decision rationales to consumers.
   - **Section 5(a) (Traceability & Audit Trails)**: Comprehensive, tamper-evident audit trails recording all model changes, evaluations, and supervisory approvals.
   - **Section 7 (Meaningful Human Oversight)**: Human-in-the-loop review, documented reviewer rationales, and challenge mechanisms for automated decisions.

```
+--------------------------------------------------------------------------------------------------+
| CBUAE REGULATORY CLAUSE        | FORKMARK CAPABILITY & EVIDENCE INVENTORY                        |
+--------------------------------------------------------------------------------------------------+
| Model Inventory & Governance   | core/model_inventory.py: Materiality tiering (HIGH/MED/LOW),   |
| (MMS Section 4)                | lifecycle status, validation schedules, owner accountability.   |
+--------------------------------------------------------------------------------------------------+
| Quantitative Validation        | core/finance_evaluators.py: Source-grounded checks for AED, SAR,|
| (MMS Section 10.4)             | Arabic-Indic digits (٠-٩), punctuation (٫, ٬), and financial %. |
+--------------------------------------------------------------------------------------------------+
| Fairness & Disparity Testing   | BiasDisparityEvaluator: Four-fifths demographic parity ratio    |
| (AI Guidance Note Section 3(c))| across Emirati nationals, GCC nationals, and expatriate groups. |
+--------------------------------------------------------------------------------------------------+
| Plain-Language Disclosure      | core/compliance_reporter.py: Structured bilingual validation    |
| (Supports AI Note Section 4(b))| memorandum providing empirical evidence for consumer disclosure.|
+--------------------------------------------------------------------------------------------------+
| Supervisory Audit Trail        | SHA-256 tamper-evident hash chaining with monotonic seq;        |
| (AI Guidance Note Section 5(a))| atomic transactional logging; pg_advisory_xact_lock on Postgres.|
+--------------------------------------------------------------------------------------------------+
| Human Oversight & Rationales   | core/store_impl/audit.py & decisions: Expert sign-off, Arabic   |
| (AI Guidance Note Section 7)   | reviewer rationale NLP tokenization, and confidence auditing.   |
+--------------------------------------------------------------------------------------------------+
| Robustness & Safety Suite      | core/safety/: 240-case bilingual red-team corpus, deterministic |
| (AI Guidance Note & MMS 10.4)  | canary/PII detectors, and stress perturbation engine.           |
+--------------------------------------------------------------------------------------------------+
| Continuous Drift Monitoring    | core/monitors/: Scheduled canary executions, atomic leases, PSI,|
| (MMS Section 9 & AI Guidance)  | median divergence, grounding rate, and automated alerts.        |
+--------------------------------------------------------------------------------------------------+
| Vendor Model Due Diligence     | Native connectors (Jais, Falcon, Azure OpenAI, OpenAI),        |
| (AI Guidance Note Principle 6) | hardware fingerprinting, and vendor due diligence records.      |
+--------------------------------------------------------------------------------------------------+
| Validation & Statistical Sound | core/statistical_analyzer.py: Paired t-test with Cohen's d_z,   |
| (MMS Sections 9, 10.4 & 10.6)  | Wilson score intervals, and Benjamini-Hochberg FDR correction.  |
+--------------------------------------------------------------------------------------------------+
| Islamic Banking Governance     | Shari'ah Supervisory Board (SSB) attestation block for          |
| (Higher Shari'ah Authority)    | Murabaha, Mudarabah, Sukuk, and Ijarah Islamic finance models.  |
+--------------------------------------------------------------------------------------------------+
```

---

## 2. On-Soil Sovereign Data Residency

Under UAE banking data protection standards (CBUAE Consumer Protection Regulation and UAE Federal Decree-Law No. 45 of 2021 on Personal Data Protection), customer data and evaluation evidence must remain strictly on-soil.

### Configuring Data Residency

ForkMark enforces regional boundaries through `DataResidencyManager` and `_DataResidencyMiddleware`:

```bash
# Set UAE (Abu Dhabi / Dubai) as the sovereign deployment default
export FM_DEFAULT_REGION="me-central-1"

# Database and cache connection strings for the UAE region
export FM_DATABASE_URL_ME_CENTRAL="postgresql://bank_admin:secure_pass@db.uae.internal:5432/mrm_prod"
export FM_REDIS_URL_ME_CENTRAL="redis://cache.uae.internal:6379/0"
```

* **Middleware Enforcement**:
  - `_DataResidencyMiddleware` inspects every incoming request header (`X-Region`).
  - If an unrecognized region is passed, the request is rejected with `400 Bad Request`.
  - If a cross-border region is requested (e.g., `X-Region: us-east-1` when configured for `me-central-1`), the middleware enforces the sovereign boundary by rejecting the request with `403 Forbidden` (`Data residency violation`).
  - Permitted requests are stamped with `X-Data-Residency-Region: me-central-1` on all HTTP responses.
* **Sovereign LLM Gateway & Pre-Socket Egress Policy**: Cloud LLM egress to external endpoints (such as `api.openai.com`, Azure OpenAI, OpenRouter, Gemini, Anthropic) is strictly mediated by `core/llm_gateway.py` with pre-socket egress blocking (`core/egress_policy.py`). When `FM_ALLOW_EXTERNAL_LLM_EGRESS=false` (the default), external HTTP/TCP socket creation is blocked before opening connections. Only internal sovereign hosts (`FM_INTERNAL_LLM_HOSTS`) are permitted.
* **Continuous Canary Scheduling & Distributed Leases**: Drift monitors execute scheduled canary runs under an atomic distributed lease (preventing duplicate runs across Uvicorn workers and Kubernetes pods). Scheduling is configurable via `FM_MONITOR_SCHEDULER=inprocess` or external cron / runner daemon (`scripts/run_monitors.py`).
* **Automated Drift Alert Webhooks**: When a continuous monitor transitions to `Alert` or `Changed` state, immediate webhook notifications are dispatched to `FM_ALERT_WEBHOOK_URL` with pre-socket validation.

---

## 3. Bilingual Model Validation Memorandum

ForkMark auto-assembles all empirical validation evidence into a structured 11-section (or 12-section for Islamic banking) **Model Validation Memorandum** aligning with MMS Section 10.6 (Reporting of Findings). While the validation memorandum is an internal supervisory validation document, it directly supports licensed financial institutions in fulfilling customer-facing plain-language disclosure under Section 4(b) of the AI Guidance Note by providing the validated empirical basis for Arabic and English explanations:

1. **Executive Summary / الملخص التنفيذي**: Model metadata, tiering, jurisdiction, and validity period.
2. **Scope & Methodology / النطاق والمنهجية**: Evaluator suite, statistical sample sizes, and a dedicated **Third-Party Model Governance** block (vendor, licence, hosting, region, cross-border status, reported models, and hardware fingerprints).
3. **Statistical Results / نتائج التحليل الإحصائي**: Wilson Score win rates, 95% confidence intervals, and paired Cohen's $d_z$ effect sizes.
4. **Bias & Fairness / تقييم التحيّز والإنصاف**: Demographic disparity ratios across nationality and demographic segments (Section 3(c)).
5. **Numerical Fidelity / التدقيق العددي والمطابقة**: Source-grounded checks against underlying loan agreements, credit policies, and product terms.
6. **Robustness & Safety / المتانة والسلامة**: Category x language matrix of Attack Success Rates (ASR), 95% Wilson intervals, champion-vs-challenger comparisons, leak detection (canary and PII), and perturbation flip rates.
7. **Ongoing Monitoring & Drift / المراقبة المستمرة**: Canary execution history, median semantic divergence, grounding pass rates, PSI stability metrics, and unresolved alert tracking.
8. **Human Review Summary / المراجعة البشرية**: Reviewer decisions with Arabic keyword theme extraction (e.g. `السياسات الائتمانية`, Section 7).
9. **Regulatory Mapping / مطابقة المتطلبات التنظيمية**: Mandatory supervisory artifact checklist including `robustness_stress_test`, `ongoing_monitoring_report`, and `vendor_due_diligence_record`.
10. **Findings & Action Plan / الملاحظات وخطة العمل**: High/Medium/Low findings generated from 9 regulatory audit rules with bilingual recommendations.
11. **Supervisory Sign-Off / الاعتماد والتوقيع الرقابي**: Formal independent validator attestation block.
12. **Shari'ah Attestation / اعتماد هيئة الرقابة الشرعية**: (For Islamic banking products) Ruling block for the Internal Shari'ah Supervisory Committee.

### Generating Reports via API or Browser

* **Interactive In-App Preview & Direct Print**: Open **Model Inventory** &rarr; select model &rarr; click **Generate Report** &rarr; switch to **Document Preview** tab &rarr; click **Print / Export PDF**. The preview fetches HTML via authenticated API call and writes directly to the print document without exposing API keys in URLs.
* **Direct REST API**:
  ```bash
  curl -X POST https://mrm.bank.internal/api/compliance/reports/credit-model-01/html \
       -H "X-API-Key: fm_live_..." \
       -H "Content-Type: application/json" \
       -d '{"framework": "cbuae_mms"}' > validation_memo.html
  ```
* **Microsoft Word (.docx)**:
  ```bash
  curl -X POST https://mrm.bank.internal/api/compliance/reports/credit-model-01/docx \
       -H "X-API-Key: fm_live_..." \
       -H "Content-Type: application/json" \
       -d '{"framework": "cbuae_mms"}' --output memo.docx
  ```

---

## 4. Tamper-Evident Audit Log Verification

For supervisory audits by CBUAE examiners or external independent validators, the audit trail's cryptographic integrity is verified via:

```bash
# Automated verification endpoint (Admin role required)
curl https://mrm.bank.internal/api/audit/verify \
     -H "X-API-Key: fm_live_admin_..."
```

**Expected JSON Response:**
```json
{
  "ok": true,
  "entries": 1420,
  "checked": 1420,
  "broken_at": null
}
```

If any supervisory record, decision, or validation result has been altered, deleted, or reordered directly in the database, `ok` returns `false` with the exact sequence number and ID of the compromised record.

---

## 5. Safety Threshold Sign-Off

The pass/fail limits used by the safety and stress suites (`GET /api/safety/thresholds`)
are defaults until the accountable model-risk owner approves them. Approval is recorded,
never assumed:

```bash
# 1. Read the thresholds in force and their fingerprint
curl https://mrm.bank.internal/api/safety/thresholds -H "X-API-Key: fm_live_admin_..."

# 2. Sign off the exact values you reviewed (admin key required)
curl -X POST https://mrm.bank.internal/api/safety/thresholds/sign-off \
     -H "X-API-Key: fm_live_admin_..." -H "Content-Type: application/json" \
     -d '{"signer_name": "Full Name", "signer_title": "Head of Model Risk",
          "acknowledged_fingerprint": "<thresholds_fingerprint from step 1>"}'
```

* The sign-off and its audit-log entry are written in one transaction.
* If the thresholds change later, the sign-off lapses and the memo says so.
* Until a sign-off exists, memo Section 6 states "Thresholds NOT signed off" and the
  findings include a MEDIUM item.

### Reviewing cases the detectors cannot decide

The safety detectors give `pass` or `fail` only when the reply is clear. A reply that
refuses and then hints at a workaround, a soft decline, or an ambiguous Shari'ah answer
is marked `needs_review` instead of guessed. Until a person rules on those cases:

* they are left out of every rate (counting them as passes would flatter the model);
* the run's verdict is `pending_review`, memo Section 6 reads PENDING_REVIEW /
  بانتظار المراجعة, and the findings include a MEDIUM item;
* a canary or PII leak still fails the run outright.

```bash
# Cases waiting for a ruling
curl "https://mrm.bank.internal/api/safety/results/<eval_run_id>/items?pending_review=true" \
     -H "X-API-Key: fm_live_reviewer_..."

# Rule on one (reviewer role or higher; a rationale of 10+ characters is required)
curl -X POST https://mrm.bank.internal/api/safety/results/item/<result_id>/review \
     -H "X-API-Key: fm_live_reviewer_..." -H "Content-Type: application/json" \
     -d '{"verdict": "fail", "rationale": "Refuses, then lists the steps anyway.",
          "reviewer_name": "Full Name"}'
```

The detector's own verdict is kept beside the ruling, and each ruling (including any
later change of mind) is written to the audit log in the same transaction.

---

## 6. Storage Architecture & Multi-Worker Production Requirements

* **SQLite (Development / Testing Only)**: SQLite is strictly single-process / single-worker. Multi-process concurrency on SQLite will cause database-level lock contention and write failures.
* **PostgreSQL (Mandatory for Production)**: Multi-worker Uvicorn configurations, Kubernetes replicas, and enterprise deployments must use PostgreSQL (`FM_DATABASE_URL`), optionally behind PgBouncer with transaction-level pooling.
