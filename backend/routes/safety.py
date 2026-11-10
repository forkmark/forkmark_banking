"""Safety and stress evaluation routes."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.deps import db, principal, ui_read_auth, ui_write_auth

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
    )
    return [SafetyResultItem(**it) for it in items]

