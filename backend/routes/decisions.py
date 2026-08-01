"""Decision listing and export endpoints (JSONL, CSV).

Human review decisions are exported as a structured audit trail for model
validation evidence (e.g. the Human Review Summary of a validation memo) — not
as fine-tuning or preference-optimization corpora.
"""
from __future__ import annotations

import csv
import io
from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.responses import StreamingResponse

from backend.deps import db, ui_read_auth

router = APIRouter(prefix="/api", tags=["decisions"])


@router.get("/decisions")
def list_decisions(workflow_id: str = Query(None),
                   eval_run_id: str = Query(None),
                   limit: int = Query(100, le=1000),
                   offset: int = Query(0, ge=0),
                   _auth=Depends(ui_read_auth)):
    return [d.to_dict() for d in db.list_decisions(workflow_id, eval_run_id, limit, offset)]


@router.get("/decisions/export")
def export_decisions(workflow_id: str = Query(None),
                     eval_run_id: str = Query(None),
                     format: str = Query("jsonl", description="Export format: jsonl or csv"),
                     _auth=Depends(ui_read_auth)):
    if not workflow_id and not eval_run_id:
        raise HTTPException(400, "Provide workflow_id or eval_run_id")

    if format == "csv":
        return _export_decisions_csv(workflow_id, eval_run_id)

    generator = db.export_decisions_jsonl(workflow_id=workflow_id, eval_run_id=eval_run_id)
    fname = f"decisions_{(eval_run_id or workflow_id)[:8]}.jsonl"
    return StreamingResponse(generator, media_type="application/x-ndjson",
                             headers={"Content-Disposition": f"attachment; filename={fname}"})


def _export_decisions_csv(workflow_id, eval_run_id):
    """Export decisions as CSV format."""
    decisions = db.list_decisions(workflow_id, eval_run_id, limit=10000, offset=0)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id", "comparison_id", "choice", "confidence", "reviewer_id",
        "rationale_for_choice", "rationale_for_rejection",
        "divergence_score", "created_at",
    ])
    for d in decisions:
        dd = d.to_dict()
        writer.writerow([
            dd.get("id", ""), dd.get("comparison_id", ""),
            dd.get("choice", ""), dd.get("confidence", ""),
            dd.get("reviewer_id", ""), dd.get("rationale_for_choice", ""),
            dd.get("rationale_for_rejection", ""),
            dd.get("divergence_score", ""), dd.get("created_at", ""),
        ])
    fname = f"decisions_{(eval_run_id or workflow_id)[:8]}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )



@router.get("/test-case-performance/{label}")
def get_test_case_performance(label: str, workflow_id: str = Query(None),
                              _auth=Depends(ui_read_auth)):
    return db.get_test_case_performance_stats(label, workflow_id=workflow_id)
