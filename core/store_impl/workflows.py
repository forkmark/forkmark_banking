"""workflows repository methods for the Forkmark data layer."""
from __future__ import annotations

from core.store_impl.base import *  # noqa: F401,F403


class WorkflowsMixin:
    def upsert_workflow(self, name: str, description: str = "", tags: list = None) -> Workflow:
        """Insert-or-update a workflow by name.

        Race-safe: if two concurrent callers try to create the same workflow
        simultaneously, the one that loses the INSERT race will catch the UNIQUE
        violation and retry with a SELECT on the next iteration.
        """
        now    = datetime.now(timezone.utc)
        new_id = str(uuid.uuid4())

        for attempt in range(2):
            try:
                with self._conn() as c:
                    row = c.fetchone("SELECT * FROM workflows WHERE name = ?", (name,))
                    if row:
                        wf = Workflow.from_row(_row(row))
                        new_desc = description if description is not None else wf.description
                        c.execute("UPDATE workflows SET description=?, updated_at=? WHERE id=?",
                                  (new_desc, now.isoformat(), wf.id))
                        wf.description = new_desc
                        wf.updated_at  = now
                        return wf
                    wf = Workflow(
                        id=new_id, name=name, description=description,
                        created_at=now, updated_at=now, tags=tags or [],
                    )
                    c.execute("""INSERT INTO workflows
                                 (id,name,description,created_at,updated_at,tags)
                                 VALUES (?,?,?,?,?,?)""",
                              (wf.id, wf.name, wf.description,
                               wf.created_at.isoformat(), wf.updated_at.isoformat(),
                               json.dumps(wf.tags)))
                    return wf
            except Exception as e:
                msg = str(e).lower()
                if ("unique" in msg or "duplicate" in msg) and attempt == 0:
                    continue   # lost the race — retry; SELECT will find the winner's row
                raise
        raise RuntimeError("upsert_workflow: exceeded retries on UNIQUE conflict")

    def get_workflow(self, wf_id: str) -> Optional[Workflow]:
        with self._conn() as c:
            r = c.fetchone("SELECT * FROM workflows WHERE id=?", (wf_id,))
        return Workflow.from_row(_row(r)) if r else None

    def list_workflows(self) -> List[Workflow]:
        """Scalar subqueries — no cartesian explosion from multi-table JOINs."""
        with self._conn() as c:
            rows = c.fetchall("""
                SELECT
                    w.*,
                    (SELECT COUNT(*) FROM workflow_runs r WHERE r.workflow_id = w.id) AS run_count,
                    (SELECT COUNT(*) FROM decisions d WHERE d.workflow_id = w.id)     AS decision_count,
                    (SELECT COUNT(*) FROM eval_runs e WHERE e.workflow_id = w.id)     AS eval_run_count
                FROM workflows w
                ORDER BY w.updated_at DESC
            """)
        return [Workflow.from_row(_row(r)) for r in rows]

    def delete_workflow(self, wf_id: str):
        with self._conn() as c:
            c.execute("DELETE FROM workflows WHERE id=?", (wf_id,))

    # ── Runs ──────────────────────────────────────────────────────────────────

    def list_runs(self, workflow_id: str, limit: int = 50, cursor: str = None) -> List[WorkflowRun]:
        filters, params = ["workflow_id=?"], [workflow_id]
        if cursor:
            filters.append("created_at < ?"); params.append(cursor)
        where = " AND ".join(filters)
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM workflow_runs WHERE {where} ORDER BY created_at DESC LIMIT ?",
                (*params, limit)
            ).fetchall()
        return [WorkflowRun.from_row(_row(r)) for r in rows]

    def create_run(self, workflow_id: str, input_data: dict = None,
                   metadata: dict = None, sdk_key_prefix: str = "",
                   eval_run_id: str = None, test_case_label: str = "") -> WorkflowRun:
        now = datetime.now(timezone.utc)
        run = WorkflowRun(
            id=str(uuid.uuid4()), workflow_id=workflow_id,
            status=RunStatus.RUNNING, created_at=now, completed_at=None,
            input_data=input_data or {}, metadata=metadata or {},
            sdk_key_prefix=sdk_key_prefix, eval_run_id=eval_run_id,
            test_case_label=test_case_label,
        )
        with self._conn() as c:
            c.execute("""INSERT INTO workflow_runs
                (id,workflow_id,status,created_at,input_data,metadata,sdk_key_prefix,
                 eval_run_id,test_case_label)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (run.id, run.workflow_id, run.status.value, run.created_at.isoformat(),
                 json.dumps(run.input_data), json.dumps(run.metadata), sdk_key_prefix,
                 eval_run_id, test_case_label))
        return run

    def complete_run(self, run_id: str, status: RunStatus = RunStatus.COMPLETED):
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            c.execute("UPDATE workflow_runs SET status=?,completed_at=? WHERE id=?",
                      (status.value, now, run_id))

    def get_run(self, run_id: str) -> Optional[WorkflowRun]:
        with self._conn() as c:
            r = c.fetchone("SELECT * FROM workflow_runs WHERE id=?", (run_id,))
        return WorkflowRun.from_row(_row(r)) if r else None


    # ── Branches ──────────────────────────────────────────────────────────────

