"""API key management endpoints (admin-only under RBAC)."""
from __future__ import annotations

import hmac
import os
from typing import Literal

from fastapi import APIRouter, HTTPException, Depends, Header, Request
from pydantic import BaseModel, Field

from backend.deps import db, ui_admin_auth, principal

router = APIRouter(prefix="/api", tags=["keys"])


class ApiKeyCreate(BaseModel):
    name: str = Field(..., max_length=256)
    role: Literal["viewer", "reviewer", "admin"] = Field(
        "admin",
        description=(
            "RBAC role for the new key: viewer (read-only), reviewer "
            "(read + record decisions / manage inventory / generate memos), or "
            "admin (full access incl. key + audit management)."
        ),
    )


def _audit(action: str, actor: str, actor_role: str, resource_id: str,
           request: Request, **detail):
    """Best-effort audit write — never breaks the primary operation."""
    try:
        ip = request.client.host if request.client else ""
        db.add_audit_log(
            action, actor=actor, actor_role=actor_role, resource_type="api_key",
            resource_id=resource_id, detail=detail, ip=ip,
        )
    except Exception:  # pragma: no cover - auditing must not fail the request
        pass


@router.get("/keys")
def list_keys(_auth=Depends(ui_admin_auth)):
    return [k.to_dict() for k in db.list_api_keys()]


@router.post("/keys", status_code=201)
def create_key(body: ApiKeyCreate, request: Request,
               x_api_key: str = Header(None, alias="X-API-Key")):
    existing = db.list_api_keys(active_only=True)
    if existing:
        if not x_api_key:
            raise HTTPException(401, "X-API-Key required to create additional keys")
        ak = db.verify_api_key(x_api_key)
        if not ak:
            raise HTTPException(401, "Invalid or revoked API key")
        # Only admins may mint keys (segregation of duties).
        if (ak.role or "").lower() != "admin":
            raise HTTPException(403, "Admin role required to create API keys.")
    else:
        bootstrap_token = os.getenv("FM_BOOTSTRAP_TOKEN")
        client_host = request.client.host if request.client else ""
        if client_host != "127.0.0.1" and client_host != "::1":
            if not bootstrap_token or not hmac.compare_digest(
                    str(x_api_key or ""), str(bootstrap_token)):
                raise HTTPException(401, "Must be on localhost or provide FM_BOOTSTRAP_TOKEN")
    new_key, raw = db.create_api_key(body.name, role=body.role)
    actor, actor_role = principal(x_api_key)
    _audit("api_key.create", actor, actor_role, new_key.id, request,
           name=body.name, role=body.role)
    return {**new_key.to_dict(), "raw_key": raw}


@router.delete("/keys/{key_id}", status_code=204)
def revoke_key(key_id: str, request: Request,
               x_api_key: str = Header(None, alias="X-API-Key"),
               _auth=Depends(ui_admin_auth)):
    db.revoke_api_key(key_id)
    actor, actor_role = principal(x_api_key)
    _audit("api_key.revoke", actor, actor_role, key_id, request)
