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
| Validation & Ongoing Monitoring| core/statistical_analyzer.py: Exact Welch t-test, Wilson score  |
| (MMS Sections 9, 10.4 & 10.6)  | confidence intervals, Cohen's d effect sizes, Benjamini-Hochberg|
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
* **Sovereign LLM Guardrail**: Cloud LLM egress to external endpoints (such as `api.openai.com`, Azure OpenAI, OpenRouter, Gemini, Anthropic) is blocked by default in `core/comparator.py` using an allowlist of internal/private hosts. Any off-premise inference requires explicit opt-in via `FM_ALLOW_EXTERNAL_LLM_EGRESS=true`.

---

## 3. Bilingual Model Validation Memorandum

ForkMark auto-assembles all empirical validation evidence into a structured 9-section (or 10-section for Islamic banking) **Model Validation Memorandum** aligning with MMS Section 10.6 (Reporting of Findings). While the validation memorandum is an internal supervisory validation document, it directly supports licensed financial institutions in fulfilling customer-facing plain-language disclosure under Section 4(b) of the AI Guidance Note by providing the validated empirical basis for Arabic and English explanations:

1. **Executive Summary / الملخص التنفيذي**: Model metadata, tiering, jurisdiction, and validity period.
2. **Scope & Methodology / النطاق والمنهجية**: Evaluator suite and statistical sample sizes.
3. **Statistical Results / نتائج التحليل الإحصائي**: Wilson Score win rates, 95% confidence intervals, and Cohen's $d$ effect sizes.
4. **Bias & Fairness / تقييم التحيّز والإنصاف**: Demographic disparity ratios across nationality and demographic segments (Section 3(c)).
5. **Numerical Fidelity / التدقيق العددي والمطابقة**: Source-grounded checks against underlying loan agreements, credit policies, and product terms.
6. **Human Review Summary / المراجعة البشرية**: Reviewer decisions with Arabic keyword theme extraction (e.g. `السياسات الائتمانية`, Section 7).
7. **Regulatory Mapping / مطابقة المتطلبات التنظيمية**: Mandatory supervisory artifact checklist.
8. **Findings & Action Plan / الملاحظات وخطة العمل**: High/Medium/Low findings with bilingual recommendations.
9. **Supervisory Sign-Off / الاعتماد والتوقيع الرقابي**: Formal independent validator attestation block.
10. **Shari'ah Attestation / اعتماد هيئة الرقابة الشرعية**: (For Islamic banking products) Ruling block for the Internal Shari'ah Supervisory Committee.

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

## 5. Storage Architecture & Multi-Worker Production Requirements

* **SQLite (Development / Testing Only)**: SQLite is strictly single-process / single-worker. Multi-process concurrency on SQLite will cause database-level lock contention and write failures.
* **PostgreSQL (Mandatory for Production)**: Multi-worker Uvicorn configurations, Kubernetes replicas, and enterprise deployments must use PostgreSQL (`FM_DATABASE_URL`), optionally behind PgBouncer with transaction-level pooling.
