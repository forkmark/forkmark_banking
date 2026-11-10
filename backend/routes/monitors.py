"""Continuous monitoring and drift detection API routes."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Set
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.deps import db, principal, ui_read_auth, ui_write_auth
from core.monitors.engine import establish_baseline_and_noise_floor, execute_monitor_run
from core.model_inventory import ModelInventory

logger = logging.getLogger("forkmark.routes.monitors")
router = APIRouter(prefix="/api", tags=["monitors"])

_BACKGROUND_TASKS: Set[asyncio.Task] = set()
_inventory = ModelInventory(db)

_TIER_INTERVALS = {
    "HIGH": 24,
    "MEDIUM": 72,
    "LOW": 168,
}


def _audit(
    action: str,
    resource_type: str,
    resource_id: str,
    request: Request,
    x_api_key: Optional[str],
    **detail,
) -> None:
    """Supervisory audit write. Refuses action if audit trail cannot be secured."""
    try:
        actor, role = principal(x_api_key)
        ip = request.client.host if request.client else ""
        db.add_audit_log(
            action,
            actor=actor,
            actor_role=role,
            resource_type=resource_type,
            resource_id=resource_id,
            detail=detail,
            ip=ip,
        )
    except Exception as exc:
        logger.error("Audit logging failed for %s on %s: %s", action, resource_id, exc)
        raise HTTPException(
            500,
            f"Audit trail write failed for {action}: supervisory action refused ({exc})",
        ) from exc


# ── Request / Response Models ────────────────────────────────────────────────


class MonitorCreate(BaseModel):
    test_set_id: str = Field(..., min_length=1)
    connector_id: str = Field(..., min_length=1)
    interval_hours: Optional[int] = Field(None, ge=1, le=8760)
    include_safety_slice: bool = True
    thresholds: Optional[Dict[str, Any]] = None


class MonitorPatch(BaseModel):
    interval_hours: Optional[int] = Field(None, ge=1, le=8760)
    include_safety_slice: Optional[bool] = None
    thresholds: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None


class ResolveRequest(BaseModel):
    rationale: str = Field(..., min_length=5, description="Auditable rationale for resolving alert")


# ── Routes ───────────────────────────────────────────────────────────────────


@router.post("/inventory/models/{model_id}/monitors", status_code=201)
async def create_monitor_for_model(
    model_id: str,
    body: MonitorCreate,
    request: Request,
    _auth=Depends(ui_write_auth),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Create a continuous drift monitor for a governed model."""
    model = _inventory.get_model(model_id)
    if not model:
        raise HTTPException(404, f"Governed model not found: {model_id}")

    # Check connector exists
    connector = db.get_provider(body.connector_id)
    if not connector:
        raise HTTPException(404, f"Connector not found: {body.connector_id}")

    # Interval default based on risk tier
    interval = body.interval_hours
    if interval is None:
        tier_val = model.risk_tier.value if hasattr(model.risk_tier, "value") else str(model.risk_tier)
        interval = _TIER_INTERVALS.get(tier_val.upper(), 24)

    actor, _ = principal(x_api_key)
    monitor_data = {
        "governed_model_id": model_id,
        "connector_id": body.connector_id,
        "test_set_id": body.test_set_id,
        "interval_hours": interval,
        "include_safety_slice": body.include_safety_slice,
        "thresholds": body.thresholds or {},
        "created_by": actor,
    }
    monitor = db.create_monitor(monitor_data)
    mid = monitor["id"]

    _audit(
        "monitor.create",
        "monitor",
        mid,
        request,
        x_api_key,
        model_id=model_id,
        connector_id=body.connector_id,
        test_set_id=body.test_set_id,
        interval_hours=interval,
    )

    # Establish baseline and noise floor in background
    async def _async_setup():
        try:
            b_id, nf = await establish_baseline_and_noise_floor(
                db=db,
                governed_model_id=model_id,
                connector_id=body.connector_id,
                test_set_id=body.test_set_id,
                include_safety_slice=body.include_safety_slice,
            )
            db.update_monitor(mid, {
                "baseline_eval_run_id": b_id,
                "noise_floor": nf,
            })
            logger.info("Established baseline and noise floor for monitor %s", mid)
        except Exception as exc:
            logger.exception("Error establishing baseline for monitor %s: %s", mid, exc)

    task = asyncio.create_task(_async_setup())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)

    return monitor


@router.get("/inventory/models/{model_id}/monitors")
def list_monitors_for_model(
    model_id: str,
    _auth=Depends(ui_read_auth),
):
    """List continuous monitors configured for a governed model."""
    model = _inventory.get_model(model_id)
    if not model:
        raise HTTPException(404, f"Governed model not found: {model_id}")
    return db.list_monitors(governed_model_id=model_id)


@router.patch("/monitors/{monitor_id}")
def update_monitor(
    monitor_id: str,
    body: MonitorPatch,
    request: Request,
    _auth=Depends(ui_write_auth),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Update monitor parameters (schedule interval, alert thresholds, enabled status)."""
    existing = db.get_monitor(monitor_id)
    if not existing:
        raise HTTPException(404, f"Monitor not found: {monitor_id}")

    updates = body.model_dump(exclude_unset=True)
    updated = db.update_monitor(monitor_id, updates)

    _audit(
        "monitor.update",
        "monitor",
        monitor_id,
        request,
        x_api_key,
        updates=updates,
    )
    return updated


@router.post("/monitors/{monitor_id}/run", status_code=202)
async def trigger_monitor_run_now(
    monitor_id: str,
    request: Request,
    _auth=Depends(ui_write_auth),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Trigger a monitor evaluation run immediately outside its recurring schedule."""
    existing = db.get_monitor(monitor_id)
    if not existing:
        raise HTTPException(404, f"Monitor not found: {monitor_id}")

    _audit("monitor.run_triggered", "monitor", monitor_id, request, x_api_key)

    try:
        run_res = await execute_monitor_run(db=db, monitor_id=monitor_id)
        return run_res
    except Exception as exc:
        logger.exception("Manual trigger of monitor %s failed: %s", monitor_id, exc)
        raise HTTPException(502, f"Monitor run execution failed: {exc}") from exc


@router.get("/monitors/{monitor_id}/runs")
def list_monitor_runs(
    monitor_id: str,
    limit: int = Query(20, ge=1, le=100),
    _auth=Depends(ui_read_auth),
):
    """Fetch run history, metrics, breaches, and resolution state for a monitor."""
    existing = db.get_monitor(monitor_id)
    if not existing:
        raise HTTPException(404, f"Monitor not found: {monitor_id}")
    return db.list_monitor_runs(monitor_id, limit=limit)


@router.post("/monitors/{monitor_id}/runs/{run_id}/resolve")
def resolve_monitor_alert(
    monitor_id: str,
    run_id: str,
    body: ResolveRequest,
    request: Request,
    _auth=Depends(ui_write_auth),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Record supervisory resolution and justification for an alert on a monitor run."""
    run = db.get_monitor_run(run_id)
    if not run or run.get("monitor_id") != monitor_id:
        raise HTTPException(404, f"Monitor run {run_id} not found on monitor {monitor_id}")

    actor, _ = principal(x_api_key)
    resolved = db.resolve_monitor_run(
        monitor_id=monitor_id,
        run_id=run_id,
        resolved_by=actor,
        rationale=body.rationale,
    )

    _audit(
        "monitor.alert_resolved",
        "monitor",
        monitor_id,
        request,
        x_api_key,
        run_id=run_id,
        rationale=body.rationale,
    )
    return resolved

