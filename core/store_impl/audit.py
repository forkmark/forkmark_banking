"""Immutable audit-log repository methods for the ForkMark data layer.

Model risk management regimes (SR 11-7, PRA SS1/23, EU AI Act, and the CBUAE
Model Management Standards) expect that changes to supervisory records — the
model inventory, human-review decisions, generated validation memos, and access
credentials — are attributable and auditable. This mixin provides a minimal,
append-only audit trail: rows are only ever inserted and read, never updated or
deleted through the application.
"""
from __future__ import annotations

import json as _json

from core.store_impl.base import *  # noqa: F401,F403


class AuditMixin:
    def add_audit_log(
        self,
        action: str,
        *,
        actor: str = "system",
        actor_role: str = "",
        resource_type: str = "",
        resource_id: str = "",
        detail: "Optional[dict]" = None,
        ip: str = "",
    ) -> str:
        """Append one immutable audit entry. Returns the new entry id.

        Best-effort by contract at the call site: auditing must never break the
        primary operation, so callers should wrap this in a try/except and log
        failures rather than propagate them.
        """
        entry_id = str(uuid.uuid4())
        with self._conn() as c:
            c.execute(
                """INSERT INTO audit_log
                   (id, ts, actor, actor_role, action, resource_type,
                    resource_id, detail, ip)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    entry_id,
                    datetime.now(timezone.utc).isoformat(),
                    actor or "system",
                    actor_role or "",
                    action,
                    resource_type or "",
                    resource_id or "",
                    _json.dumps(detail or {}),
                    ip or "",
                ),
            )
        return entry_id

    def list_audit_log(
        self,
        limit: int = 100,
        offset: int = 0,
        action: "Optional[str]" = None,
        resource_type: "Optional[str]" = None,
    ) -> "List[dict]":
        """Return audit entries, newest first, with optional filters."""
        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        clauses, params = [], []
        if action:
            clauses.append("action = ?")
            params.append(action)
        if resource_type:
            clauses.append("resource_type = ?")
            params.append(resource_type)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([limit, offset])
        with self._read_conn() as c:
            rows = c.fetchall(
                f"SELECT * FROM audit_log{where} "
                f"ORDER BY ts DESC LIMIT ? OFFSET ?",
                tuple(params),
            )
        out = []
        for raw in rows:
            r = _row(raw)
            # _json_load accepts a JSON string (SQLite/TEXT) or an already-parsed
            # dict (PostgreSQL/JSONB), so this is dialect-agnostic.
            r["detail"] = _json_load(r.get("detail"), default={})
            out.append(r)
        return out
