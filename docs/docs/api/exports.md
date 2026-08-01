# Export &amp; compliance report formats

ForkMark exports human-review decisions as an **audit trail for model validation
evidence** and generates **model validation memoranda**. It does not produce
fine-tuning or preference-optimization corpora.

## Raw decisions

All decision fields — choice, confidence, rationale, divergence — as JSONL or CSV:

```bash
curl "http://localhost:7700/api/decisions/export?workflow_id=WF_ID&format=jsonl" \
  -H "X-API-Key: fm_..."
curl "http://localhost:7700/api/decisions/export?workflow_id=WF_ID&format=csv" \
  -H "X-API-Key: fm_..."
```

## Review decision corpus

A structured audit trail of reviewer decisions — reviewer metadata, confidence,
structured rationale, divergence score, and data category — suitable for the
Human Review Summary section of a validation memo.

```bash
curl "http://localhost:7700/api/eval-runs/{er_id}/export/preference-corpus?anonymize=true" \
  -H "X-API-Key: fm_..."
```

**Query parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `workflow_id` | string | — | Filter by workflow |
| `anonymize` | bool | `true` | Replace raw prompts with provenance hashes |
| `require_consent` | bool | `false` | Skip workflows without an active data-consent record |

## Validation memo

Generate a full model validation memorandum for a model under a chosen framework,
as JSON or a formatted `.docx`. Evidence (statistical comparisons, bias and
numerical-fidelity checks, and recorded human decisions) is assembled from the
request and the platform.

```bash
# JSON memo
curl -X POST "http://localhost:7700/api/compliance/reports/MODEL_ID" \
  -H "X-API-Key: fm_..." -H "Content-Type: application/json" \
  -d '{"framework": "eu_ai_act", "workflow_id": "WF_ID"}'

# .docx download
curl -X POST "http://localhost:7700/api/compliance/reports/MODEL_ID/docx" \
  -H "X-API-Key: fm_..." -H "Content-Type: application/json" \
  -d '{"framework": "sr_11_7"}' -o validation_memo.docx

# Prior reports for a model
curl "http://localhost:7700/api/compliance/reports/MODEL_ID/history" \
  -H "X-API-Key: fm_..."
```

The JSON memo has nine sections: executive summary, scope &amp; methodology,
statistical results, bias &amp; fairness, numerical fidelity, human review summary,
regulatory mapping, findings &amp; recommendations, and sign-off.

## Data consent

Exports that touch review data can be gated by consent records:

```bash
curl -X POST http://localhost:7700/api/consent \
  -H "X-API-Key: fm_..." \
  -H "Content-Type: application/json" \
  -d '{
    "scope": "workflow",
    "workflow_id": "WF_ID",
    "consent_type": "training_data",
    "granted_by": "legal@company.com",
    "notes": "Approved for internal audit export"
  }'
```

Consent types: `training_data`, `anonymized_export`, `aggregated_stats`.
