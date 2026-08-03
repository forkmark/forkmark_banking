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
from core.store_impl.base import _AUDIT_GENESIS_HASH, _audit_entry_hash


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

        Each entry is chained to the previous one with a SHA-256 (``prev_hash`` +
        ``entry_hash``, ordered by ``seq``), making the append-only log
        tamper-evident — see :meth:`verify_audit_chain`.
        """
        entry_id = str(uuid.uuid4())
        ts = datetime.now(timezone.utc).isoformat()
        detail = detail or {}
        with self._conn() as c:
            head = c.fetchone(
                "SELECT seq, entry_hash FROM audit_log ORDER BY seq DESC LIMIT 1"
            )
            if head is not None:
                h = _row(head)
                last_seq = h.get("seq") or 0
                prev_hash = h.get("entry_hash") or _AUDIT_GENESIS_HASH
            else:
                last_seq = 0
                prev_hash = _AUDIT_GENESIS_HASH
            seq = int(last_seq) + 1
            entry_hash = _audit_entry_hash(
                prev_hash, entry_id, ts, actor or "system", actor_role or "",
                action, resource_type or "", resource_id or "", detail, ip or "",
            )
            c.execute(
                """INSERT INTO audit_log
                   (id, ts, actor, actor_role, action, resource_type,
                    resource_id, detail, ip, seq, prev_hash, entry_hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    entry_id,
                    ts,
                    actor or "system",
                    actor_role or "",
                    action,
                    resource_type or "",
                    resource_id or "",
                    _json.dumps(detail),
                    ip or "",
                    seq,
                    prev_hash,
                    entry_hash,
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

    def verify_audit_chain(self) -> dict:
        """Recompute the audit-log hash chain and report whether it is intact.

        Returns a dict with ``ok`` (bool), ``entries`` (rows examined),
        ``checked`` (rows verified before any break), and ``broken_at`` — the
        ``{seq, id}`` of the first entry whose content or linkage fails to
        verify, or ``None`` when the whole chain is valid. Any out-of-band edit,
        delete, or reorder of a supervisory record is detected here, which is the
        evidence that the append-only log has not been altered.
        """
        with self._read_conn() as c:
            rows = c.fetchall("SELECT * FROM audit_log ORDER BY seq ASC")
        prev = _AUDIT_GENESIS_HASH
        checked = 0
        for raw in rows:
            r = _row(raw)
            expected = _audit_entry_hash(
                r.get("prev_hash") or _AUDIT_GENESIS_HASH, r.get("id"),
                r.get("ts"), r.get("actor"), r.get("actor_role"),
                r.get("action"), r.get("resource_type"), r.get("resource_id"),
                r.get("detail"), r.get("ip"),
            )
            if ((r.get("prev_hash") or _AUDIT_GENESIS_HASH) != prev
                    or (r.get("entry_hash") or "") != expected):
                return {
                    "ok": False,
                    "entries": len(rows),
                    "checked": checked,
                    "broken_at": {"seq": r.get("seq"), "id": r.get("id")},
                }
            prev = r.get("entry_hash") or ""
            checked += 1
        return {"ok": True, "entries": len(rows), "checked": checked,
                "broken_at": None}
