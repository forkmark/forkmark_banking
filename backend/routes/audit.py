"""Audit-log endpoints — the immutable trail of who changed what, and when.

Exposes a read-only view of the append-only ``audit_log`` (API-key management,
model-inventory mutations, and validation-memo generation). Admin role only:
the audit trail is a supervisory record and must not be broadly readable or
mutable. There is deliberately no write or delete endpoint — entries are only
ever appended by the operations they record.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from backend.deps import db, ui_admin_auth

router = APIRouter(prefix="/api", tags=["audit"])


class AuditEntry(BaseModel):
    id: str
    ts: str
    actor: str
    actor_role: str = ""
    action: str
    resource_type: str = ""
    resource_id: str = ""
    detail: Dict[str, Any] = Field(default_factory=dict)
    ip: str = ""


@router.get(
    "/audit/log",
    response_model=List[AuditEntry],
    summary="List audit-log entries (admin only)",
)
def list_audit(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    action: Optional[str] = Query(None, description="Filter by action, e.g. 'model.create'."),
    resource_type: Optional[str] = Query(None, description="Filter by resource type, e.g. 'model'."),
    _auth: object = Depends(ui_admin_auth),
) -> List[AuditEntry]:
    """Return audit entries newest-first, optionally filtered by action or
    resource type. Requires an admin API key when authentication is enabled."""
    rows = db.list_audit_log(
        limit=limit, offset=offset, action=action, resource_type=resource_type
    )
    return [AuditEntry(**r) for r in rows]
