"""Eval run UI endpoints."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Optional, List

from backend.deps import db, ui_read_auth, ui_write_auth
from backend.response_models import EvalRunResponse
from core.models import EvalRunStatus

router = APIRouter(prefix="/api", tags=["eval-runs"])
_BACKGROUND_TASKS: set[asyncio.Task] = set()


class EvalRunCreateBody(BaseModel):
    workflow_name: str = Field(..., max_length=256)
    name: str = Field(..., max_length=256)
    description: str = Field("", max_length=2000)
    branch_a_config: dict = {}
    branch_b_config: dict = {}
    test_set_id: Optional[str] = Field(None, max_length=64)
    total_cases: int = 0
    governed_model_id: Optional[str] = Field(
        None, max_length=200,
        description="Model in the governed inventory this validation run evidences.")


class EvalRunCompleteBody(BaseModel):
    status: str = "completed"
    total_cases: Optional[int] = None


class GovernedModelLink(BaseModel):
    model_config = {"protected_namespaces": ()}
    model_id: str = Field(..., max_length=200, min_length=1)


@router.get("/eval-runs", response_model=List[EvalRunResponse])
def list_eval_runs(workflow_id: str = Query(None), limit: int = Query(50, le=200),
                   _auth=Depends(ui_read_auth)):
    ers = db.list_eval_runs(workflow_id, limit)
    if not ers:
        return []
    batch_stats = db.batch_eval_run_stats([er.id for er in ers])
    return [{**er.to_dict(), "stats": batch_stats[er.id]} for er in ers]


@router.post("/eval-runs", status_code=201)
def create_eval_run(body: EvalRunCreateBody, _auth=Depends(ui_write_auth)):
    wf = db.upsert_workflow(body.workflow_name)
    er = db.create_eval_run(
        workflow_id=wf.id, name=body.name, description=body.description,
        branch_a_config=body.branch_a_config, branch_b_config=body.branch_b_config,
        test_set_id=body.test_set_id, total_cases=body.total_cases,
        governed_model_id=body.governed_model_id,
    )
    return er.to_dict()


@router.post(
    "/eval-runs/{er_id}/governed-model",
    summary="Link a validation run to a model in the governed inventory",
)
def link_governed_model(er_id: str, body: GovernedModelLink,
                        _auth=Depends(ui_write_auth)):
    """Attach this validation run to a governed model.

    This is what lets ``POST /api/compliance/reports/{model_id}`` auto-assemble
    its evidence: the memo reads the model's linked runs, then computes
    numerical-fidelity over their champion-vs-challenger outputs and pulls the
    recorded human-review decisions. Without a link the memo has nothing to
    draw on and comes back empty.
    """
    if not db.get_eval_run(er_id):
        raise HTTPException(404, "Eval run not found")
    from core.model_inventory import ModelInventory
    if not ModelInventory(db).get_model(body.model_id):
        raise HTTPException(404, f"Model not found in inventory: {body.model_id}")
    db.set_eval_run_governed_model(er_id, body.model_id)
    er = db.get_eval_run(er_id)
    return er.to_dict()


@router.get("/eval-runs/{er_id}")
def get_eval_run(er_id: str, _auth=Depends(ui_read_auth)):
    er = db.get_eval_run(er_id)
    if not er:
        raise HTTPException(404, "Eval run not found")
    stats = db.get_eval_run_stats(er_id)
    return {**er.to_dict(), "stats": stats}


@router.delete("/eval-runs/{er_id}", status_code=204)
def delete_eval_run(er_id: str, _auth=Depends(ui_write_auth)):
    db.delete_eval_run(er_id)


@router.patch("/eval-runs/{er_id}/complete")
def complete_eval_run(er_id: str, body: EvalRunCompleteBody, _auth=Depends(ui_write_auth)):
    er = db.get_eval_run(er_id)
    if not er:
        raise HTTPException(404, "Eval run not found")
    try:
        status = EvalRunStatus(body.status)
    except ValueError:
        raise HTTPException(400, f"Invalid status: {body.status}")
    db.update_eval_run_status(er_id, status, total_cases=body.total_cases)
    return {"ok": True}


@router.get("/eval-runs/{er_id}/export")
def export_eval_run_decisions(er_id: str, _auth=Depends(ui_read_auth)):
    generator = db.export_decisions_jsonl(eval_run_id=er_id)
    return StreamingResponse(
        generator, media_type="application/x-ndjson",
        headers={"Content-Disposition": f"attachment; filename=eval_{er_id[:8]}_decisions.jsonl"},
    )


@router.get("/eval-runs/{er_id}/review-stats")
def get_review_stats(er_id: str, _auth=Depends(ui_read_auth)):
    return db.get_review_stats(er_id)


@router.post(
    "/eval-runs/{er_id}/resume",
    summary="Resume an interrupted or failed evaluation run without re-executing completed cases",
)
async def resume_eval_run(er_id: str, _auth=Depends(ui_write_auth)):
    """Resume an evaluation run from where it left off, skipping already completed cases."""
    er = db.get_eval_run(er_id)
    if not er:
        raise HTTPException(404, "Eval run not found")
    if er.status == EvalRunStatus.COMPLETED:
        return {"eval_run_id": er_id, "status": "already_completed", "message": "Evaluation run is already completed"}

    test_cases: List[dict] = []
    if er.test_set_id:
        raw_cases = db.list_test_cases(er.test_set_id) if hasattr(db, "list_test_cases") else []
        for tc in raw_cases:
            inp = getattr(tc, "input_data", {})
            p = inp.get("input") if isinstance(inp, dict) else (inp or getattr(tc, "label", ""))
            test_cases.append({
                "id": getattr(tc, "id", ""),
                "label": getattr(tc, "label", ""),
                "input": p,
                "input_data": inp,
                "expected_output": getattr(tc, "expected_output", ""),
            })

    if not test_cases and er.run_kind in ("safety", "stress"):
        from core.monitors.engine import get_safety_slice_cases
        corpus_path = Path(__file__).resolve().parent.parent.parent / "fixtures" / "safety" / "forkmark_redteam_seed_v0_1.json"
        if corpus_path.is_file():
            c_data = json.loads(corpus_path.read_text(encoding="utf-8"))
            for c in c_data.get("cases", []):
                test_cases.append({
                    "id": c["id"],
                    "label": c["id"],
                    "input_data": {
                        "input": c["turns"][0]["content"] if c.get("turns") else "",
                        "system_prompt": c.get("system_prompt", ""),
                        "context_document": c.get("context_document", ""),
                        "category": c.get("category", ""),
                        "language": c.get("language", "en"),
                        "severity": c.get("severity", "HIGH"),
                        "expected": c.get("expected", "refuse"),
                    },
                    "prompt": c["turns"][0]["content"] if c.get("turns") else "",
                })
        else:
            for sc in get_safety_slice_cases():
                test_cases.append({"id": sc["id"], "label": sc["id"], "input_data": sc})

    if not test_cases:
        raise HTTPException(400, "No test cases found to resume eval run")

    from core.suite_runner import run_suite_async
    task = asyncio.create_task(
        run_suite_async(
            db=db,
            eval_run_id=er_id,
            workflow_id=er.workflow_id,
            test_cases=test_cases,
            champion=er.branch_a_config or {},
            challenger=er.branch_b_config or None,
            governed_model_id=er.governed_model_id,
            run_kind=er.run_kind or "ab",
            resume=True,
        )
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)

    return {"eval_run_id": er_id, "status": "resumed", "total_cases": len(test_cases)}
