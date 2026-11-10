"""Model Gateway call log repository methods for the ForkMark data layer."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from core.store_impl.base import *  # noqa: F401,F403


class GatewayMixin:
    """Storage methods for the Model Gateway egress call log."""

    def log_gateway_call(
        self,
        purpose: str,
        host: str,
        decision: str,
        *,
        call_id: Optional[str] = None,
        connector_id: Optional[str] = None,
        is_internal: bool = False,
        model: str = "",
        http_status: int = 0,
        latency_ms: int = 0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cross_border: bool = False,
        eval_run_id: Optional[str] = None,
        actor: str = "system",
        ts: Optional[str] = None,
        conn: Any = None,
    ) -> str:
        """Record an outbound model call (allowed, blocked, or error).

        Records metadata only — NEVER prompt or output text.
        """
        call_id = call_id or str(uuid.uuid4())
        ts = ts or datetime.now(timezone.utc).isoformat()

        def _insert(c) -> str:
            c.execute(
                """INSERT INTO gateway_call_log (
                    id, ts, purpose, connector_id, host, is_internal,
                    decision, model, http_status, latency_ms,
                    tokens_in, tokens_out, cross_border, eval_run_id, actor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    call_id,
                    ts,
                    purpose or "",
                    connector_id or "",
                    host or "",
                    1 if is_internal else 0,
                    decision or "allowed",
                    model or "",
                    http_status or 0,
                    latency_ms or 0,
                    tokens_in or 0,
                    tokens_out or 0,
                    1 if cross_border else 0,
                    eval_run_id,
                    actor or "system",
                ),
            )
            return call_id

        if conn is not None:
            return _insert(conn)

        with self._conn() as c:
            return _insert(c)

    def list_gateway_calls(
        self,
        purpose: Optional[str] = None,
        decision: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        eval_run_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[dict]:
        """Return gateway call log entries, newest first, with optional filters."""
        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        clauses, params = [], []
        if purpose:
            clauses.append("purpose = ?")
            params.append(purpose)
        if decision:
            clauses.append("decision = ?")
            params.append(decision)
        if date_from:
            clauses.append("ts >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("ts <= ?")
            params.append(date_to)
        if eval_run_id:
            clauses.append("eval_run_id = ?")
            params.append(eval_run_id)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([limit, offset])

        with self._read_conn() as c:
            rows = c.fetchall(
                f"SELECT * FROM gateway_call_log{where} ORDER BY ts DESC LIMIT ? OFFSET ?",
                tuple(params),
            )
        return [_row(r) for r in rows]

    def get_gateway_call(self, call_id: str) -> Optional[dict]:
        """Retrieve a single gateway call record by ID."""
        with self._read_conn() as c:
            row = c.fetchone("SELECT * FROM gateway_call_log WHERE id = ?", (call_id,))
            return _row(row) if row else None

