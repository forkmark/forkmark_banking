"""trajectories repository methods for the Forkmark data layer."""
from __future__ import annotations

from core.store_impl.base import *  # noqa: F401,F403


class TrajectoriesMixin:
    def create_trace_event(self, **kw) -> None:
        """Insert a single trace event."""
        from core.agent_models import TraceEvent
        kw.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        for json_field in ("input_data", "output_data", "metadata"):
            if json_field in kw and isinstance(kw[json_field], dict):
                kw[json_field] = json.dumps(kw[json_field])
        cols = ", ".join(kw.keys())
        placeholders = ", ".join("?" for _ in kw)
        with self._conn() as c:
            c.execute(
                f"INSERT INTO trace_events ({cols}) VALUES ({placeholders})",
                tuple(kw.values()),
            )

    def create_trace_events_batch(self, events: List[dict]) -> None:
        """Bulk-insert trace events."""
        if not events:
            return
        for ev in events:
            ev.setdefault("created_at", datetime.now(timezone.utc).isoformat())
            for json_field in ("input_data", "output_data", "metadata"):
                if json_field in ev and isinstance(ev[json_field], dict):
                    ev[json_field] = json.dumps(ev[json_field])
        cols = sorted(events[0].keys())
        placeholders = ", ".join("?" for _ in cols)
        col_str = ", ".join(cols)
        with self._conn() as c:
            for ev in events:
                vals = tuple(ev.get(k) for k in cols)
                c.execute(
                    f"INSERT INTO trace_events ({col_str}) VALUES ({placeholders})",
                    vals,
                )

    def get_trace_events(self, branch_id: str = None, run_id: str = None,
                         parent_event_id: str = "__UNSET__") -> list:
        """Retrieve trace events with flexible filtering.

        Parameters
        ----------
        branch_id : str, optional
            Filter by branch.
        run_id : str, optional
            Filter by workflow run.
        parent_event_id : str
            Filter by parent. Pass None to get root events only.
            Pass "__UNSET__" (default) to skip this filter.
        """
        from core.agent_models import TraceEvent
        clauses, params = [], []
        if branch_id:
            clauses.append("branch_id = ?")
            params.append(branch_id)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        if parent_event_id != "__UNSET__":
            if parent_event_id is None:
                clauses.append("parent_event_id IS NULL")
            else:
                clauses.append("parent_event_id = ?")
                params.append(parent_event_id)
        where = " AND ".join(clauses) if clauses else "1=1"
        with self._read_conn() as c:
            rows = c.fetchall(
                f"SELECT * FROM trace_events WHERE {where} ORDER BY event_index",
                tuple(params),
            )
        return [TraceEvent.from_row(_row(r)) for r in rows]

    def create_trajectory_outcome(self, **kw) -> None:
        """Insert a trajectory outcome record."""
        kw.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        for json_field in ("tool_sequence_detail", "outcome_detail", "efficiency_detail"):
            if json_field in kw and isinstance(kw[json_field], dict):
                kw[json_field] = json.dumps(kw[json_field])
        cols = ", ".join(kw.keys())
        placeholders = ", ".join("?" for _ in kw)
        with self._conn() as c:
            c.execute(
                f"INSERT INTO trajectory_outcomes ({cols}) VALUES ({placeholders})",
                tuple(kw.values()),
            )

    def get_trajectory_outcomes(self, comparison_id: str = None,
                                 workflow_id: str = None,
                                 run_id: str = None) -> list:
        """Retrieve trajectory outcomes with flexible filtering."""
        from core.agent_models import TrajectoryOutcome
        clauses, params = [], []
        if comparison_id:
            clauses.append("comparison_id = ?")
            params.append(comparison_id)
        if workflow_id:
            clauses.append("workflow_id = ?")
            params.append(workflow_id)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        where = " AND ".join(clauses) if clauses else "1=1"
        with self._read_conn() as c:
            rows = c.fetchall(
                f"SELECT * FROM trajectory_outcomes WHERE {where} ORDER BY created_at DESC",
                tuple(params),
            )
        return [TrajectoryOutcome.from_row(_row(r)) for r in rows]
