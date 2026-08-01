"""Admin endpoints (pruning, maintenance)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from backend.deps import db, ui_admin_auth, ui_write_auth

router = APIRouter(prefix="/api", tags=["admin"])


class PruneBody(BaseModel):
    older_than_days: int = 30


@router.delete("/admin/prune", status_code=200)
def admin_prune(body: PruneBody, _auth=Depends(ui_admin_auth)):
    # Destructive maintenance — admin only (deletes historical step outputs).
    if body.older_than_days < 1:
        raise HTTPException(400, "older_than_days must be >= 1")
    deleted = db.prune_step_outputs(body.older_than_days)
    try:
        db.add_audit_log("admin.prune", resource_type="step_outputs",
                         detail={"older_than_days": body.older_than_days,
                                 "deleted_rows": deleted})
    except Exception:  # pragma: no cover
        pass
    return {"deleted_rows": deleted, "older_than_days": body.older_than_days}


@router.post("/admin/rescore-pending", status_code=200)
async def admin_rescore_pending(_auth=Depends(ui_write_auth)):
    """Re-enqueue scoring for comparisons stuck in 'pending'/'running' (e.g. after
    a crash or restart). Runs the same sweep performed automatically at startup."""
    from core.background import recover_pending_scoring
    n = await recover_pending_scoring(db)
    return {"requeued": n}
