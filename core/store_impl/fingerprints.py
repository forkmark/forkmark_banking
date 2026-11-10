"""model_fingerprints repository methods for tracking model provenance."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from core.store_impl.base import _row


class FingerprintsMixin:
    """Methods for recording and querying model fingerprints."""

    def record_fingerprint(
        self,
        governed_model_id: str,
        connector_id: str,
        model_reported: str,
        system_fingerprint: str = "",
        source: str = "validation_run",
        eval_run_id: Optional[str] = None,
    ) -> dict:
        """Record an observed model report and fingerprint."""
        now = datetime.now(timezone.utc).isoformat()
        fid = str(uuid.uuid4())
        with self._conn() as c:
            c.execute(
                """INSERT INTO model_fingerprints
                (id, governed_model_id, connector_id, observed_at,
                 model_reported, system_fingerprint, source, eval_run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fid,
                    governed_model_id,
                    connector_id,
                    now,
                    model_reported or "",
                    system_fingerprint or "",
                    source,
                    eval_run_id,
                ),
            )
        return {
            "id": fid,
            "governed_model_id": governed_model_id,
            "connector_id": connector_id,
            "observed_at": now,
            "model_reported": model_reported or "",
            "system_fingerprint": system_fingerprint or "",
            "source": source,
            "eval_run_id": eval_run_id,
        }

    def list_fingerprints(
        self,
        governed_model_id: Optional[str] = None,
        connector_id: Optional[str] = None,
        limit: int = 50,
        model_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List observed fingerprints, newest first."""
        target_id = governed_model_id or model_id
        query = "SELECT * FROM model_fingerprints WHERE 1=1"
        params: list[Any] = []
        if target_id:
            query += " AND governed_model_id=?"
            params.append(target_id)
        if connector_id:
            query += " AND connector_id=?"
            params.append(connector_id)
        query += " ORDER BY observed_at DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))

        with self._read_conn() as c:
            rows = c.fetchall(query, tuple(params))
        return [_row(r) for r in rows]

    def list_model_fingerprints(
        self,
        governed_model_id: Optional[str] = None,
        connector_id: Optional[str] = None,
        limit: int = 50,
        model_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Alias for list_fingerprints."""
        return self.list_fingerprints(
            governed_model_id=governed_model_id,
            connector_id=connector_id,
            limit=limit,
            model_id=model_id,
        )

    def get_latest_fingerprint(
        self, governed_model_id: str
    ) -> Optional[Dict[str, Any]]:
        """Get the most recently observed fingerprint for a governed model."""
        with self._read_conn() as c:
            row = c.fetchone(
                "SELECT * FROM model_fingerprints WHERE governed_model_id=? ORDER BY observed_at DESC LIMIT 1",
                (governed_model_id,),
            )
        return _row(row) if row else None

