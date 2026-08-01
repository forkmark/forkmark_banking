"""Compliance reporting endpoints — model validation memos.

Assembles ForkMark's evidence (statistical comparison results, bias and
numerical-fidelity checks, and recorded human review decisions) into a structured
model validation memorandum aligned to a regulatory framework, available as JSON
or as a formatted .docx for the validator's evidence pack. A lightweight history
of generated reports is retained per model.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.deps import db, principal, ui_read_auth, ui_write_auth
from core.compliance_reporter import ComplianceReporter, ValidationEvidence
from core.finance_evaluators import BiasDisparityEvaluator, NumericalFidelityEvaluator
from core.regulatory_frameworks import RegulatoryFramework
from core.statistical_analyzer import analyze, analyze_batch

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["compliance"])


# ── Schemas ─────────────────────────────────────────────────────────────────


class ScorePair(BaseModel):
    scores_a: List[float] = Field(..., min_length=2)
    scores_b: List[float] = Field(..., min_length=2)


class NumericalCheck(BaseModel):
    source_document: str
    model_output: str


class ComplianceReportRequest(BaseModel):
    framework: RegulatoryFramework = Field(
        ...,
        description=(
            "Framework to assess against: sr_11_7, eu_ai_act, pra_ss1_23, cbuae "
            "(2026 AI guidance), or cbuae_mms (2022 Model Management Standards)."
        ),
    )
    statistical_comparisons: Optional[List[ScorePair]] = None
    bias_groups: Optional[Dict[str, float]] = Field(
        None, description="Per-group aggregate scores for disparity assessment."
    )
    bias_threshold: float = Field(1.2, ge=1.0)
    numerical_checks: Optional[List[NumericalCheck]] = None
    workflow_id: Optional[str] = Field(
        None, description="Pull recorded human review decisions from this workflow."
    )
    evaluator_suite: Optional[List[str]] = None


class ValidationMemoResponse(BaseModel):
    model_config = {"extra": "allow"}

    title: str
    generated_at: str
    executive_summary: Dict[str, Any]
    scope_and_methodology: Dict[str, Any]
    statistical_results: List[Dict[str, Any]]
    bias_and_fairness: Dict[str, Any]
    numerical_fidelity: Dict[str, Any]
    human_review_summary: Dict[str, Any]
    regulatory_mapping: Dict[str, Any]
    findings_and_recommendations: List[Dict[str, Any]]
    sign_off: Dict[str, Any]


class ReportHistoryItem(BaseModel):
    id: str
    model_id: str
    framework: str
    generated_at: str
    findings_count: int
    coverage_complete: bool
    format: str


# ── Evidence assembly + history persistence ──────────────────────────────────


def _build_evidence(body: ComplianceReportRequest) -> ValidationEvidence:
    """Assemble a ValidationEvidence bundle from the request inputs."""
    stat_results = []
    if body.statistical_comparisons:
        pairs = [(p.scores_a, p.scores_b) for p in body.statistical_comparisons]
        stat_results = (
            analyze_batch(pairs) if len(pairs) > 1 else [analyze(*pairs[0])]
        )

    bias_results = []
    if body.bias_groups:
        bias_results = [
            BiasDisparityEvaluator(threshold=body.bias_threshold).evaluate(body.bias_groups)
        ]

    fidelity_results = [
        NumericalFidelityEvaluator().evaluate(c.source_document, c.model_output)
        for c in (body.numerical_checks or [])
    ]

    decisions: List[dict[str, Any]] = []
    if body.workflow_id:
        decisions = [
            d.to_dict()
            for d in db.list_decisions(body.workflow_id, limit=1000, offset=0)
        ]

    return ValidationEvidence(
        statistical_results=stat_results,
        bias_results=bias_results,
        numerical_fidelity_results=fidelity_results,
        decisions=decisions,
        evaluator_suite=body.evaluator_suite or [],
    )


def _evidence_is_empty(ev: ValidationEvidence) -> bool:
    """True when the caller supplied no evidence in the request body."""
    return not (
        ev.statistical_results or ev.bias_results
        or ev.numerical_fidelity_results or ev.decisions
    )


def _build_evidence_from_model(model_id: str) -> ValidationEvidence:
    """Auto-assemble validation evidence from a governed model's linked eval runs.

    This is what makes the memo evidence-backed by default: it pulls the model's
    recorded human-review (effective-challenge) decisions and computes a real
    numerical-fidelity / grounding check over each comparison's champion-vs-
    challenger outputs — no hand-fed evidence required. The approved champion
    output is treated as the source of truth; any figure the challenger introduces
    that the champion did not ground is flagged.
    """
    runs = db.list_eval_runs_for_model(model_id)
    if not runs:
        return ValidationEvidence()

    # Aggregate evidence across EVERY linked validation run, not just the most
    # recent. Taking only runs[0] silently dropped all prior evidence: linking a
    # newer, empty run would blank the memo with no warning.
    nf_eval = NumericalFidelityEvaluator()
    fidelity_results = []
    decisions: list[dict[str, Any]] = []
    for er in runs:
        # Decisions are filtered by eval_run_id so runs sharing a workflow are
        # not double-counted.
        decisions.extend(
            d.to_dict()
            for d in db.list_decisions(eval_run_id=er.id, limit=1000, offset=0)
        )
        for comp in db.list_comparisons(eval_run_id=er.id, limit=1000):
            champion = " ".join(
                s.output_text for s in db.get_step_outputs_for_branch(comp.branch_a_id)
            )
            challenger = " ".join(
                s.output_text for s in db.get_step_outputs_for_branch(comp.branch_b_id)
            )
            if champion.strip() and challenger.strip():
                fidelity_results.append(nf_eval.evaluate(champion, challenger))

    # Runs come back newest-first; the validation window spans the whole set.
    run_times = [r.created_at for r in runs if r.created_at]
    period_start = min(run_times) if run_times else None
    period_end = max(run_times) if run_times else None
    logger.info(
        "memo evidence for %s assembled from %d linked run(s): %s",
        model_id, len(runs), ", ".join(r.id for r in runs),
    )

    # Fairness: compute a real disparity + verdict from the model's observed
    # per-group scores (ingested from the fairness test set).
    bias_results = []
    from core.model_inventory import ModelInventory
    model = ModelInventory(db).get_model(model_id)
    groups = (model.evaluation_signals or {}).get("bias_groups") if model else None
    if groups and len(groups) >= 2:
        bias_results.append(BiasDisparityEvaluator().evaluate(groups))

    suite = ["NumericalFidelityEvaluator"]
    if bias_results:
        suite.append("BiasDisparityEvaluator")
    suite.append("human_effective_challenge")

    return ValidationEvidence(
        numerical_fidelity_results=fidelity_results,
        bias_results=bias_results,
        decisions=decisions,
        evaluator_suite=suite,
        validation_period_start=period_start,
        validation_period_end=period_end,
    )


def _record_report(
    model_id: str, framework: RegulatoryFramework, memo: dict[str, Any], fmt: str
) -> None:
    """Persist report metadata to the compliance_reports history table."""
    mapping = memo["regulatory_mapping"]
    findings = memo["findings_and_recommendations"]
    with db._conn() as c:
        c.execute(
            """INSERT INTO compliance_reports
               (id, model_id, framework, generated_at, findings_count,
                coverage_complete, format)
               VALUES (?,?,?,?,?,?,?)""",
            (
                uuid.uuid4().hex,
                model_id,
                framework.value,
                memo["generated_at"],
                len(findings),
                1 if mapping["coverage_complete"] else 0,
                fmt,
            ),
        )


def _audit_report(
    model_id: str, framework: RegulatoryFramework, memo: dict[str, Any],
    fmt: str, request: Request, x_api_key: "str | None",
) -> None:
    """Best-effort audit write when a validation memo is generated."""
    try:
        actor, role = principal(x_api_key)
        ip = request.client.host if request.client else ""
        db.add_audit_log(
            "compliance.report_generated", actor=actor, actor_role=role,
            resource_type="model", resource_id=model_id,
            detail={
                "framework": framework.value,
                "format": fmt,
                "findings": len(memo["findings_and_recommendations"]),
                "coverage_complete": memo["regulatory_mapping"]["coverage_complete"],
            },
            ip=ip,
        )
    except Exception:  # pragma: no cover - auditing must not fail the request
        pass


# ── Routes ──────────────────────────────────────────────────────────────────


@router.post(
    "/compliance/reports/{model_id}",
    response_model=ValidationMemoResponse,
    summary="Generate a model validation memo (JSON)",
)
def generate_report(
    model_id: str,
    body: ComplianceReportRequest,
    request: Request,
    x_api_key: str = Header(None, alias="X-API-Key"),
    _auth: object = Depends(ui_write_auth),
) -> ValidationMemoResponse:
    """Generate a structured model validation memorandum for the given model under
    the selected framework (CBUAE MMS / CBUAE AI / UAE joint / EU AI Act / SR 26-2 / PRA).

    When the request supplies no explicit evidence, evidence is auto-assembled from
    the model's linked validation runs (decisions + computed numerical-fidelity)."""
    reporter = ComplianceReporter(db)
    try:
        evidence = _build_evidence(body)
        if _evidence_is_empty(evidence):
            evidence = _build_evidence_from_model(model_id)
        memo = reporter.generate_validation_memo(model_id, body.framework, evidence)
    except KeyError as exc:
        raise HTTPException(404, f"Model not found: {model_id}") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    _record_report(model_id, body.framework, memo, "json")
    _audit_report(model_id, body.framework, memo, "json", request, x_api_key)
    return ValidationMemoResponse(**memo)


@router.post(
    "/compliance/reports/{model_id}/docx",
    summary="Generate a model validation memo (.docx download)",
)
def generate_report_docx(
    model_id: str,
    body: ComplianceReportRequest,
    request: Request,
    x_api_key: str = Header(None, alias="X-API-Key"),
    _auth: object = Depends(ui_write_auth),
) -> FileResponse:
    """Generate the validation memo as a formatted .docx file for download."""
    reporter = ComplianceReporter(db)
    try:
        evidence = _build_evidence(body)
        if _evidence_is_empty(evidence):
            evidence = _build_evidence_from_model(model_id)
        memo = reporter.generate_validation_memo(model_id, body.framework, evidence)
        path = reporter.generate_validation_memo_docx(model_id, body.framework, evidence)
    except KeyError as exc:
        raise HTTPException(404, f"Model not found: {model_id}") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    _record_report(model_id, body.framework, memo, "docx")
    _audit_report(model_id, body.framework, memo, "docx", request, x_api_key)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"validation_memo_{model_id}_{body.framework.value}_{stamp}.docx"
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename,
    )


@router.get(
    "/compliance/reports/{model_id}/history",
    response_model=List[ReportHistoryItem],
    summary="List prior validation reports for a model",
)
def report_history(
    model_id: str, _auth: object = Depends(ui_read_auth)
) -> List[ReportHistoryItem]:
    """Return the history of validation memos generated for the model, newest first."""
    with db._read_conn() as c:
        rows = c.fetchall(
            "SELECT * FROM compliance_reports WHERE model_id = ? "
            "ORDER BY generated_at DESC",
            (model_id,),
        )
    return [
        ReportHistoryItem(
            id=r["id"],
            model_id=r["model_id"],
            framework=r["framework"],
            generated_at=r["generated_at"],
            findings_count=r["findings_count"],
            coverage_complete=bool(r["coverage_complete"]),
            format=r["format"],
        )
        for r in rows
    ]
