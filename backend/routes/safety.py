"""Safety and stress evaluation routes."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.deps import db, principal, ui_admin_auth, ui_read_auth, ui_write_auth

router = APIRouter(prefix="/api/safety", tags=["safety"])

CORPUS_PATH = Path(__file__).resolve().parent.parent.parent / "fixtures" / "safety" / "forkmark_redteam_seed_v0_1.json"


class SafetyCorpusInstallResponse(BaseModel):
    test_set_id: str
    name: str
    version: int
    case_count: int
    is_frozen: bool


class SafetyResultItem(BaseModel):
    id: str
    eval_run_id: str
    governed_model_id: Optional[str] = None
    test_case_id: str
    case_key: str
    branch: str
    category: str
    language: str
    severity: str
    expected: str
    verdict: str
    detectors: Dict[str, Any] = Field(default_factory=dict)
    judge: Dict[str, Any] = Field(default_factory=dict)
    reply_excerpt: str = ""
    created_at: str
    effective_verdict: Optional[str] = None
    review_verdict: Optional[str] = None
    review_rationale: Optional[str] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[str] = None


class SafetyRunSummaryResponse(BaseModel):
    eval_run_id: str
    verdict: str
    pooled_asr: float
    pooled_ci: List[float]
    over_refusal_rate: float
    over_refusal_ci: List[float]
    canary_leaks: int
    pii_leaks: int
    category_metrics: Dict[str, Any]
    language_metrics: Dict[str, Any]
    challenger_comparison: Dict[str, Any]
    breaches: List[str]
    thresholds_applied: Dict[str, Any]
    total_results: int
    stress_flip_rate: float = 0.0
    stability_score: float = 0.0
    figure_consistency_rate: float = 1.0
    bilingual_figure_match_rate: float = 1.0
    cases_needing_review: int = 0
    cases_reviewed: int = 0
    total_cases: int = 0


@router.post(
    "/corpus/install",
    status_code=201,
    response_model=SafetyCorpusInstallResponse,
    summary="Install the ForkMark Red-Team Seed Corpus (admin only)",
)
def install_safety_corpus(
    request: Request,
    x_api_key: str = Header(None, alias="X-API-Key"),
    _auth: object = Depends(ui_write_auth),
) -> SafetyCorpusInstallResponse:
    """Load the versioned 240-case red-team safety corpus into a frozen, versioned test set."""
    _actor, role = principal(x_api_key)
    if role != "admin":
        raise HTTPException(403, "Only admin can install the safety corpus")

    if not CORPUS_PATH.is_file():
        raise HTTPException(500, f"Safety corpus file not found at {CORPUS_PATH}")

    data = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    cases = data.get("cases", [])
    version_str = data.get("version", "0.1")

    # Check if already installed
    existing = [ts for ts in db.list_test_sets() if ts.name == f"ForkMark Red-Team Safety Corpus v{version_str}"]
    if existing:
        ts = existing[0]
        return SafetyCorpusInstallResponse(
            test_set_id=ts.id,
            name=ts.name,
            version=getattr(ts, "version", 1),
            case_count=getattr(ts, "case_count", len(cases)),
            is_frozen=getattr(ts, "is_frozen", True),
        )

    ts = db.create_test_set(
        name=f"ForkMark Red-Team Safety Corpus v{version_str}",
        description="Standardised banking-specific red-team safety, jailbreak, and adversarial seed corpus (EN/MSA/Gulf/Arabizi).",
    )

    # Bulk load cases
    tc_batch = []
    for c in cases:
        tc_batch.append({
            "label": c["id"],
            "input_data": {
                "input": c["turns"][0]["content"] if c.get("turns") else "",
                "system_prompt": c.get("system_prompt", ""),
                "context_document": c.get("context_document", ""),
                "category": c.get("category", ""),
                "language": c.get("language", "en"),
                "severity": c.get("severity", "HIGH"),
                "expected": c.get("expected", "refuse"),
                "forbidden_patterns": c.get("forbidden_patterns", []),
                "required_patterns": c.get("required_patterns", []),
                "notes": c.get("notes", ""),
            },
            "tags": ["safety", "redteam", c.get("category", ""), c.get("language", "")],
            "domain": "banking",
            "industry": "financial_services",
            "use_case_type": "safety",
            "failure_mode": c.get("category", ""),
            "test_goal": c.get("notes", ""),
        })

    db.bulk_add_test_cases(ts.id, tc_batch)
    db.freeze_test_set(ts.id)

    db.add_audit_log(
        action="safety_corpus.install",
        actor=_actor,
        actor_role=role,
        resource_type="test_set",
        resource_id=ts.id,
        detail={"name": ts.name, "case_count": len(cases), "version": version_str},
    )

    return SafetyCorpusInstallResponse(
        test_set_id=ts.id,
        name=ts.name,
        version=1,
        case_count=len(cases),
        is_frozen=True,
    )


@router.get(
    "/results/{eval_run_id}",
    response_model=SafetyRunSummaryResponse,
    summary="Get aggregated safety summary and detector metrics for an evaluation run",
)
def get_safety_summary(
    eval_run_id: str,
    _auth: object = Depends(ui_read_auth),
) -> SafetyRunSummaryResponse:
    """Retrieve category x language safety results and threshold checks for an eval run."""
    summary = db.get_safety_summary(eval_run_id)
    raw_results = db.list_safety_results(eval_run_id=eval_run_id, limit=5000)

    return SafetyRunSummaryResponse(
        eval_run_id=eval_run_id,
        verdict=summary.get("verdict", "pass"),
        pooled_asr=summary.get("pooled_asr", 0.0),
        pooled_ci=list(summary.get("pooled_ci", (0.0, 0.0))),
        over_refusal_rate=summary.get("over_refusal_rate", 0.0),
        over_refusal_ci=list(summary.get("over_refusal_ci", (0.0, 0.0))),
        canary_leaks=summary.get("canary_leaks", 0),
        pii_leaks=summary.get("pii_leaks", 0),
        category_metrics=summary.get("category_metrics", {}),
        language_metrics=summary.get("language_metrics", {}),
        challenger_comparison=summary.get("challenger_comparison", {}),
        breaches=summary.get("breaches", []),
        thresholds_applied=summary.get("thresholds_applied", {}),
        total_results=len(raw_results),
        stress_flip_rate=summary.get("stress_flip_rate", 0.0),
        stability_score=summary.get("stability_score", 0.0),
        figure_consistency_rate=summary.get("figure_consistency_rate", 1.0),
        bilingual_figure_match_rate=summary.get("bilingual_figure_match_rate", 1.0),
        cases_needing_review=summary.get("cases_needing_review", 0),
        cases_reviewed=summary.get("cases_reviewed", 0),
        total_cases=summary.get("total_cases", 0),
    )


@router.get(
    "/results/{eval_run_id}/items",
    response_model=List[SafetyResultItem],
    summary="List per-case safety results for an evaluation run",
)
def list_safety_result_items(
    eval_run_id: str,
    category: Optional[str] = Query(None),
    language: Optional[str] = Query(None),
    branch: Optional[str] = Query(None),
    pending_review: bool = Query(False, description="Only cases the detectors sent to human review that nobody has ruled on yet"),
    limit: int = Query(500, ge=1, le=5000),
    _auth: object = Depends(ui_read_auth),
) -> List[SafetyResultItem]:
    """List detailed detector outputs and excerpts for individual cases in a safety run."""
    items = db.list_safety_results(
        eval_run_id=eval_run_id,
        category=category,
        language=language,
        branch=branch,
        limit=limit,
        pending_review=pending_review,
    )
    return [SafetyResultItem(**it) for it in items]


class SafetyReviewRequest(BaseModel):
    verdict: str = Field(..., description="'pass' or 'fail' — the reviewer's final ruling on this case")
    rationale: str = Field(..., description="Why; at least 10 characters. Shown in the audit log.")
    reviewer_name: str = Field(..., description="Full name of the person making the ruling")


@router.post(
    "/results/item/{result_id}/review",
    response_model=SafetyResultItem,
    summary="Record a named reviewer's verdict on one safety case",
)
def review_safety_result_item(
    result_id: str,
    body: SafetyReviewRequest,
    api_key: Optional[str] = Depends(ui_write_auth),
) -> SafetyResultItem:
    """Reviewer role or higher. The detector's verdict is kept; the review is audit-logged."""
    if not api_key:
        # Even in open mode a ruling must be attributable to a key.
        raise HTTPException(401, "A reviewer API key is required so the ruling is attributable.")
    if not body.reviewer_name.strip():
        raise HTTPException(422, "reviewer_name is required.")
    key_prefix, role = principal(api_key)
    try:
        item = db.review_safety_result(
            result_id,
            verdict=body.verdict,
            rationale=body.rationale,
            reviewer=f"{body.reviewer_name.strip()} ({key_prefix})",
            reviewer_role=role,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return SafetyResultItem(**item)



# ── Threshold sign-off ─────────────────────────────────────────────────────────

class ThresholdSignOffRequest(BaseModel):
    signer_name: str = Field(..., description="Full name of the person giving the sign-off")
    signer_title: str = Field(..., description="Their role, e.g. Head of Model Risk")
    acknowledged_fingerprint: str = Field(
        ..., description="thresholds_fingerprint from GET /api/safety/thresholds, confirming which values were reviewed")
    notes: str = ""


@router.get("/thresholds")
async def get_safety_thresholds(_auth=Depends(ui_read_auth)):
    """The safety thresholds in force and whether a named validator has signed them off."""
    from core.safety.signoff import get_signoff_status, thresholds_fingerprint
    from core.safety.thresholds import DEFAULT_THRESHOLDS
    return {
        "thresholds": DEFAULT_THRESHOLDS,
        "thresholds_fingerprint": thresholds_fingerprint(),
        "sign_off": get_signoff_status(db),
    }


@router.post("/thresholds/sign-off")
async def sign_off_safety_thresholds(
    body: ThresholdSignOffRequest,
    _auth=Depends(ui_admin_auth),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Record a named validator's sign-off of the current thresholds (admin only, audit-logged)."""
    from core.safety.signoff import record_signoff
    if not x_api_key:
        # A sign-off must be attributable even when the UI runs in open mode.
        raise HTTPException(401, "An admin API key is required to sign off thresholds.")
    actor, role = principal(x_api_key)
    try:
        return record_signoff(
            db,
            signer_name=body.signer_name,
            signer_title=body.signer_title,
            acknowledged_fingerprint=body.acknowledged_fingerprint,
            notes=body.notes,
            actor=actor,
            actor_role=role,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
