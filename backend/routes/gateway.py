"""Model Gateway telemetry and connector routes."""
from __future__ import annotations

from typing import List, Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from backend.deps import db, ui_admin_auth, ui_read_auth
from core.connectors.presets import list_presets

router = APIRouter(prefix="/api", tags=["gateway"])


class GatewayCallRecord(BaseModel):
    id: str
    ts: str
    purpose: str
    connector_id: Optional[str] = ""
    host: str = ""
    is_internal: bool = False
    decision: str = "allowed"
    model: str = ""
    http_status: int = 0
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cross_border: bool = False
    eval_run_id: Optional[str] = None
    actor: str = "system"


@router.get("/gateway/log", response_model=List[GatewayCallRecord])
def get_gateway_call_log(
    purpose: Optional[str] = None,
    decision: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    eval_run_id: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    _auth=Depends(ui_admin_auth),
):
    """Retrieve audit log of all outbound model calls passing through ModelGateway (admin only)."""
    rows = db.list_gateway_calls(
        purpose=purpose,
        decision=decision,
        date_from=from_date,
        date_to=to_date,
        eval_run_id=eval_run_id,
        limit=limit,
        offset=offset,
    )
    # Cast SQLite 0/1 integers to booleans
    for r in rows:
        r["is_internal"] = bool(r.get("is_internal"))
        r["cross_border"] = bool(r.get("cross_border"))
    return rows


@router.get("/connectors/presets")
def get_connector_presets(_auth=Depends(ui_read_auth)):
    """Retrieve standard sovereign and commercial connector presets."""
    return list_presets()

