"""monitors and monitor_runs repository methods for the Forkmark data layer."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from core.store_impl.base import _row


class MonitorsMixin:
    """Methods for continuous drift monitoring, scheduling, and lease management."""

    def create_monitor(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new monitor record."""
        mid = data.get("id") or f"mon-{uuid.uuid4().hex[:10]}"
        now = datetime.now(timezone.utc).isoformat()
        governed_model_id = data["governed_model_id"]
        connector_id = data["connector_id"]
        test_set_id = data["test_set_id"]
        baseline_eval_run_id = data.get("baseline_eval_run_id")
        noise_floor = json.dumps(data.get("noise_floor") or {})
        interval_hours = int(data.get("interval_hours") or 24)
        include_safety_slice = 1 if data.get("include_safety_slice", True) else 0
        thresholds = json.dumps(data.get("thresholds") or {})
        enabled = 1 if data.get("enabled", True) else 0
        state = data.get("state") or "OK"
        created_by = data.get("created_by") or "system"
        next_run_at = data.get("next_run_at") or (
            datetime.now(timezone.utc) + timedelta(hours=interval_hours)
        ).isoformat()

        with self._conn() as c:
            c.execute(
                """INSERT INTO monitors (
                    id, governed_model_id, connector_id, test_set_id,
                    baseline_eval_run_id, noise_floor, interval_hours,
                    include_safety_slice, thresholds, enabled, state,
                    last_run_at, next_run_at, lease_owner, lease_expires_at,
                    created_at, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mid,
                    governed_model_id,
                    connector_id,
                    test_set_id,
                    baseline_eval_run_id,
                    noise_floor,
                    interval_hours,
                    include_safety_slice,
                    thresholds,
                    enabled,
                    state,
                    data.get("last_run_at"),
                    next_run_at,
                    None,
                    None,
                    now,
                    created_by,
                ),
            )
        return self.get_monitor(mid)  # type: ignore

    def get_monitor(self, monitor_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a monitor by ID."""
        with self._read_conn() as c:
            row = c.fetchone("SELECT * FROM monitors WHERE id = ?", (monitor_id,))
        if not row:
            return None
        res = _row(row)
        res["noise_floor"] = json.loads(res.get("noise_floor") or "{}")
        res["thresholds"] = json.loads(res.get("thresholds") or "{}")
        res["include_safety_slice"] = bool(res.get("include_safety_slice"))
        res["enabled"] = bool(res.get("enabled"))
        return res

    def list_monitors(
        self,
        governed_model_id: Optional[str] = None,
        model_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List monitors, optionally filtered by governed_model_id (or alias model_id)."""
        target_model_id = governed_model_id or model_id
        with self._read_conn() as c:
            if target_model_id:
                rows = c.fetchall(
                    "SELECT * FROM monitors WHERE governed_model_id = ? ORDER BY created_at DESC",
                    (target_model_id,),
                )
            else:
                rows = c.fetchall("SELECT * FROM monitors ORDER BY created_at DESC")
        result = []
        for r in rows:
            m = _row(r)
            m["noise_floor"] = json.loads(m.get("noise_floor") or "{}")
            m["thresholds"] = json.loads(m.get("thresholds") or "{}")
            m["include_safety_slice"] = bool(m.get("include_safety_slice"))
            m["enabled"] = bool(m.get("enabled"))
            result.append(m)
        return result

    def update_monitor(self, monitor_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Update monitor fields (interval, thresholds, enabled, baseline, noise_floor, etc.)."""
        allowed = [
            "interval_hours",
            "thresholds",
            "enabled",
            "baseline_eval_run_id",
            "noise_floor",
            "include_safety_slice",
            "state",
            "last_run_at",
            "next_run_at",
        ]
        set_parts = []
        params = []
        for k, v in updates.items():
            if k not in allowed:
                continue
            if k in ("thresholds", "noise_floor"):
                set_parts.append(f"{k} = ?")
                params.append(json.dumps(v or {}))
            elif k in ("enabled", "include_safety_slice"):
                set_parts.append(f"{k} = ?")
                params.append(1 if v else 0)
            else:
                set_parts.append(f"{k} = ?")
                params.append(v)

        if not set_parts:
            return self.get_monitor(monitor_id)

        params.append(monitor_id)
        with self._conn() as c:
            c.execute(f"UPDATE monitors SET {', '.join(set_parts)} WHERE id = ?", tuple(params))
        return self.get_monitor(monitor_id)

    def acquire_monitor_lease(
        self,
        monitor_id: str,
        lease_owner: str,
        lease_duration_seconds: int = 1800,
        now_iso: Optional[str] = None,
    ) -> bool:
        """Atomically claim a distributed lease for a monitor.

        Guarantees that two concurrent runners can never run the same monitor.
        Returns True if the lease was acquired, False otherwise.
        """
        now = now_iso or datetime.now(timezone.utc).isoformat()
        now_dt = datetime.fromisoformat(now)
        expires_at = (now_dt + timedelta(seconds=lease_duration_seconds)).isoformat()

        with self._conn() as c:
            cur = c.execute(
                """UPDATE monitors
                   SET lease_owner = ?, lease_expires_at = ?
                   WHERE id = ? AND enabled = 1
                     AND (lease_expires_at IS NULL OR lease_expires_at < ?)""",
                (lease_owner, expires_at, monitor_id, now),
            )
            rowcount = getattr(cur, "rowcount", 0)
            return rowcount > 0

    def release_monitor_lease(
        self,
        monitor_id: str,
        lease_owner: str,
        next_run_at: Optional[str] = None,
        last_run_at: Optional[str] = None,
        state: Optional[str] = None,
    ) -> bool:
        """Release the lease held by lease_owner and optionally advance run schedule."""
        with self._conn() as c:
            cur = c.execute(
                """UPDATE monitors
                   SET lease_owner = NULL,
                       lease_expires_at = NULL,
                       next_run_at = COALESCE(?, next_run_at),
                       last_run_at = COALESCE(?, last_run_at),
                       state = COALESCE(?, state)
                   WHERE id = ? AND lease_owner = ?""",
                (next_run_at, last_run_at, state, monitor_id, lease_owner),
            )
            rowcount = getattr(cur, "rowcount", 0)
            return rowcount > 0

    def list_due_monitors(self, now_iso: Optional[str] = None) -> List[Dict[str, Any]]:
        """List enabled monitors that are due to run and not currently leased."""
        now = now_iso or datetime.now(timezone.utc).isoformat()
        with self._read_conn() as c:
            rows = c.fetchall(
                """SELECT * FROM monitors
                   WHERE enabled = 1
                     AND (next_run_at IS NULL OR next_run_at <= ?)
                     AND (lease_expires_at IS NULL OR lease_expires_at < ?)
                   ORDER BY next_run_at ASC""",
                (now, now),
            )
        result = []
        for r in rows:
            m = _row(r)
            m["noise_floor"] = json.loads(m.get("noise_floor") or "{}")
            m["thresholds"] = json.loads(m.get("thresholds") or "{}")
            m["include_safety_slice"] = bool(m.get("include_safety_slice"))
            m["enabled"] = bool(m.get("enabled"))
            result.append(m)
        return result

    def create_monitor_run(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Record the start of a monitor execution run."""
        rid = data.get("id") or f"mrun-{uuid.uuid4().hex[:10]}"
        now = datetime.now(timezone.utc).isoformat()
        monitor_id = data["monitor_id"]
        eval_run_id = data.get("eval_run_id")
        started_at = data.get("started_at") or now
        status = data.get("status") or "running"
        state = data.get("state") or "OK"
        metrics = json.dumps(data.get("metrics") or {})
        breaches = json.dumps(data.get("breaches") or [])
        model_reported = data.get("model_reported")
        system_fingerprint = data.get("system_fingerprint")

        with self._conn() as c:
            c.execute(
                """INSERT INTO monitor_runs (
                    id, monitor_id, eval_run_id, started_at, finished_at,
                    status, state, metrics, breaches, model_reported,
                    system_fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    rid,
                    monitor_id,
                    eval_run_id,
                    started_at,
                    data.get("finished_at"),
                    status,
                    state,
                    metrics,
                    breaches,
                    model_reported,
                    system_fingerprint,
                ),
            )
        return self.get_monitor_run(rid)  # type: ignore

    def save_monitor_run(self, *args, **kwargs) -> Dict[str, Any]:
        """Save a monitor execution run, accepting a dictionary or keyword arguments."""
        if args and isinstance(args[0], dict):
            return self.create_monitor_run(args[0])
        return self.create_monitor_run(kwargs)

    def get_monitor_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a monitor run by ID."""
        with self._read_conn() as c:
            row = c.fetchone("SELECT * FROM monitor_runs WHERE id = ?", (run_id,))
        if not row:
            return None
        res = _row(row)
        res["metrics"] = json.loads(res.get("metrics") or "{}")
        res["breaches"] = json.loads(res.get("breaches") or "[]")
        return res

    def list_monitor_runs(self, monitor_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """List historical runs for a monitor."""
        with self._read_conn() as c:
            rows = c.fetchall(
                "SELECT * FROM monitor_runs WHERE monitor_id = ? ORDER BY started_at DESC LIMIT ?",
                (monitor_id, limit),
            )
        result = []
        for r in rows:
            m = _row(r)
            m["metrics"] = json.loads(m.get("metrics") or "{}")
            m["breaches"] = json.loads(m.get("breaches") or "[]")
            result.append(m)
        return result

    def update_monitor_run(self, run_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Update a monitor run upon completion or status change."""
        allowed = [
            "finished_at",
            "status",
            "state",
            "metrics",
            "breaches",
            "model_reported",
            "system_fingerprint",
            "eval_run_id",
        ]
        set_parts = []
        params = []
        for k, v in updates.items():
            if k not in allowed:
                continue
            if k in ("metrics", "breaches"):
                set_parts.append(f"{k} = ?")
                params.append(json.dumps(v))
            else:
                set_parts.append(f"{k} = ?")
                params.append(v)

        if not set_parts:
            return self.get_monitor_run(run_id)

        params.append(run_id)
        with self._conn() as c:
            c.execute(f"UPDATE monitor_runs SET {', '.join(set_parts)} WHERE id = ?", tuple(params))
        return self.get_monitor_run(run_id)

    def resolve_monitor_run(
        self, monitor_id: str, run_id: str, resolved_by: str, rationale: str
    ) -> Optional[Dict[str, Any]]:
        """Record the resolution rationale of an alert on a monitor run."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            c.execute(
                """UPDATE monitor_runs
                   SET resolved_at = ?, resolved_by = ?, resolution_rationale = ?
                   WHERE id = ? AND monitor_id = ?""",
                (now, resolved_by, rationale, run_id, monitor_id),
            )
        return self.get_monitor_run(run_id)

    def update_monitor_and_model_state_atomic(
        self,
        monitor_id: str,
        state: str,
        governed_model_id: str,
        audit_entry: Optional[Dict[str, Any]] = None,
        next_validation_due: Optional[str] = None,
        now_utc: Optional[Any] = None,
        run_id: Optional[str] = None,
        **extra: Any,
    ) -> bool:
        """Atomically update monitor state, model next_validation_due, and write audit log.

        Guarantees that if audit write fails, all state changes roll back.
        """
        now_dt = now_utc if isinstance(now_utc, datetime) else datetime.now(timezone.utc)
        now = now_dt.isoformat()
        if next_validation_due is None and state.lower() in ("changed", "alert"):
            next_validation_due = now

        entry = audit_entry or {
            "action": f"monitor.{state.lower()}",
            "actor": "system",
            "actor_role": "system",
            "detail": {"monitor_id": monitor_id, "state": state, "run_id": run_id},
        }

        with self._conn() as c:
            # 1. Update monitor state
            c.execute("UPDATE monitors SET state = ? WHERE id = ?", (state, monitor_id))

            # 2. Update model inventory if needed (e.g. Changed sets next_validation_due to today)
            if next_validation_due is not None:
                c.execute(
                    """UPDATE model_inventory
                       SET next_validation_due = ?, updated_at = ?
                       WHERE model_id = ?""",
                    (next_validation_due, now, governed_model_id),
                )

            # 3. Write supervisory audit log inside the EXACT same transaction
            self.add_audit_log(
                action=entry.get("action", f"monitor.{state.lower()}"),
                actor=entry.get("actor", "system"),
                actor_role=entry.get("actor_role", "system"),
                resource_type="model",
                resource_id=governed_model_id,
                detail=entry.get("detail", {}),
                ip=entry.get("ip", ""),
                conn=c,
            )
        return True
