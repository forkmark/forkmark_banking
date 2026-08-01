"""evalruns repository methods for the Forkmark data layer."""
from __future__ import annotations

from core.store_impl.base import *  # noqa: F401,F403


class EvalRunsMixin:
    def create_eval_run(self, workflow_id: str, name: str,
                        branch_a_config: dict, branch_b_config: dict,
                        description: str = "", test_set_id: str = None,
                        total_cases: int = 0, governed_model_id: str = None) -> EvalRun:
        now = datetime.now(timezone.utc)
        er = EvalRun(
            id=str(uuid.uuid4()), workflow_id=workflow_id, name=name,
            description=description, test_set_id=test_set_id,
            branch_a_config=branch_a_config, branch_b_config=branch_b_config,
            status=EvalRunStatus.PENDING, total_cases=total_cases, created_at=now,
            governed_model_id=governed_model_id,
        )
        with self._conn() as c:
            c.execute("""
                INSERT INTO eval_runs
                (id,workflow_id,name,description,test_set_id,governed_model_id,
                 branch_a_config,branch_b_config,status,total_cases,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (er.id, er.workflow_id, er.name, er.description, er.test_set_id,
                 er.governed_model_id,
                 json.dumps(er.branch_a_config), json.dumps(er.branch_b_config),
                 er.status.value, er.total_cases, er.created_at.isoformat()),
            )
            c.execute("UPDATE workflows SET eval_run_count=eval_run_count+1 WHERE id=?",
                      (workflow_id,))
        return er

    def list_eval_runs_for_model(self, governed_model_id: str,
                                 limit: int = 50) -> List[EvalRun]:
        """Eval (validation) runs linked to a governed model, newest first."""
        with self._conn() as c:
            rows = c.fetchall(
                "SELECT * FROM eval_runs WHERE governed_model_id=? "
                "ORDER BY created_at DESC LIMIT ?", (governed_model_id, limit))
        return [EvalRun.from_row(_row(r)) for r in rows]

    def set_eval_run_governed_model(self, er_id: str, governed_model_id: str):
        """Attach (or re-attach) a governed model to an existing eval run."""
        with self._conn() as c:
            c.execute("UPDATE eval_runs SET governed_model_id=? WHERE id=?",
                      (governed_model_id, er_id))

    def get_eval_run(self, er_id: str) -> Optional[EvalRun]:
        with self._conn() as c:
            r = c.fetchone("SELECT * FROM eval_runs WHERE id=?", (er_id,))
        return EvalRun.from_row(_row(r)) if r else None

    def list_eval_runs(self, workflow_id: str = None, limit: int = 50) -> List[EvalRun]:
        with self._conn() as c:
            if workflow_id:
                rows = c.fetchall(
                    "SELECT * FROM eval_runs WHERE workflow_id=? ORDER BY created_at DESC LIMIT ?",
                    (workflow_id, limit))
            else:
                rows = c.fetchall(
                    "SELECT * FROM eval_runs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [EvalRun.from_row(_row(r)) for r in rows]

    def update_eval_run_status(self, er_id: str, status: EvalRunStatus,
                               total_cases: int = None):
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            if status in (EvalRunStatus.COMPLETED, EvalRunStatus.FAILED):
                c.execute(
                    "UPDATE eval_runs SET status=?, completed_at=? WHERE id=?",
                    (status.value, now, er_id))
            else:
                c.execute("UPDATE eval_runs SET status=? WHERE id=?", (status.value, er_id))
            if total_cases is not None:
                c.execute("UPDATE eval_runs SET total_cases=? WHERE id=?", (total_cases, er_id))

    def get_eval_run_stats(self, er_id: str, comp_limit: int = 500) -> dict:
        """Aggregate stats for an eval run.

        Uses two queries:
          1. A full aggregate query (no LIMIT) for correct counts and averages.
          2. A limited query for the ``comparisons`` list shown in the UI.

        Args:
            comp_limit: Max comparisons to return in the ``comparisons`` list.
                        Does NOT affect total/decided/avg_divergence aggregates.
        """
        with self._conn() as c:
            # ── Aggregate query — full scan, no LIMIT ──────────────────────────
            agg_rows = c.fetchall("""
                SELECT
                    c.decided,
                    c.divergence_score,
                    d.choice,
                    d.confidence
                FROM comparisons c
                LEFT JOIN decisions d ON c.decision_id = d.id
                WHERE c.eval_run_id = ?
            """, (er_id,))
            agg_list = [_row(r) for r in agg_rows]

            # ── Limited list for UI display ────────────────────────────────────
            list_rows = c.fetchall("""
                SELECT
                    c.id,
                    c.decided,
                    c.divergence_score,
                    c.decision_id,
                    c.test_case_label,
                    d.choice,
                    d.confidence
                FROM comparisons c
                LEFT JOIN decisions d ON c.decision_id = d.id
                WHERE c.eval_run_id = ?
                ORDER BY COALESCE(c.divergence_score, -1) DESC
                LIMIT ?
            """, (er_id, comp_limit))
            comp_list = [_row(r) for r in list_rows]

            # ── Token totals per branch (for cost estimation in UI) ───────────
            tok_rows = c.fetchall("""
                SELECT 'A' AS side,
                       COALESCE(SUM(so.tokens_input),  0) AS tin,
                       COALESCE(SUM(so.tokens_output), 0) AS tout
                FROM step_outputs so
                JOIN comparisons   cp ON so.branch_id = cp.branch_a_id
                WHERE cp.eval_run_id = ?
                UNION ALL
                SELECT 'B' AS side,
                       COALESCE(SUM(so.tokens_input),  0) AS tin,
                       COALESCE(SUM(so.tokens_output), 0) AS tout
                FROM step_outputs so
                JOIN comparisons   cp ON so.branch_id = cp.branch_b_id
                WHERE cp.eval_run_id = ?
            """, (er_id, er_id))
            tok_map = {_row(r)["side"]: {"tokens_in": _row(r)["tin"], "tokens_out": _row(r)["tout"]} for r in tok_rows}

        total   = len(agg_list)
        decided = sum(1 for c in agg_list if c.get("decided"))
        scores  = [c["divergence_score"] for c in agg_list if c["divergence_score"] is not None]
        avg_div = round(sum(scores) / len(scores), 4) if scores else None

        buckets = [0, 0, 0, 0, 0]
        for s in scores:
            if not (0.0 <= s <= 1.0):
                continue  # skip malformed scores outside [0,1]
            idx = min(int(s * 5), 4)
            buckets[idx] += 1

        choice_breakdown = {"A": 0, "B": 0, "neither": 0, "both": 0}
        conf_breakdown   = {"high": 0, "medium": 0, "low": 0}
        for comp in agg_list:
            if comp.get("choice"):
                choice_breakdown[comp["choice"]] = choice_breakdown.get(comp["choice"], 0) + 1
            if comp.get("confidence"):
                conf_breakdown[comp["confidence"]] = conf_breakdown.get(comp["confidence"], 0) + 1

        return {
            "total":                total,
            "decided":              decided,
            "pending":              total - decided,
            "avg_divergence":       avg_div,
            "divergence_buckets":   buckets,
            "choice_breakdown":     choice_breakdown,
            "confidence_breakdown": conf_breakdown,
            "comparisons":          comp_list,   # includes choice field — no N+1 in UI
            "tokens_a":             tok_map.get("A", {}).get("tokens_in",  0),
            "tokens_a_out":         tok_map.get("A", {}).get("tokens_out", 0),
            "tokens_b":             tok_map.get("B", {}).get("tokens_in",  0),
            "tokens_b_out":         tok_map.get("B", {}).get("tokens_out", 0),
        }

    def batch_eval_run_stats(self, er_ids: List[str]) -> Dict[str, dict]:
        """Return lightweight stats for multiple eval runs in a single query.

        Used by list_eval_runs to avoid one get_eval_run_stats() call per row.
        Returns a dict keyed by eval_run_id.
        """
        if not er_ids:
            return {}
        placeholders = ",".join("?" * len(er_ids))
        with self._conn() as c:
            rows = c.fetchall(f"""
                SELECT
                    c.eval_run_id,
                    COUNT(c.id)                         AS total,
                    SUM(c.decided)                      AS decided,
                    AVG(c.divergence_score)             AS avg_divergence
                FROM comparisons c
                WHERE c.eval_run_id IN ({placeholders})
                GROUP BY c.eval_run_id
            """, er_ids)
        result: Dict[str, dict] = {}
        for r in rows:
            r = _row(r)
            eid = r["eval_run_id"]
            total   = r["total"]   or 0
            decided = r["decided"] or 0
            result[eid] = {
                "total":          total,
                "decided":        decided,
                "pending":        total - decided,
                "avg_divergence": round(r["avg_divergence"], 4) if r["avg_divergence"] is not None else None,
            }
        # Fill zeros for any er_id that had no comparisons yet
        for eid in er_ids:
            if eid not in result:
                result[eid] = {"total": 0, "decided": 0, "pending": 0, "avg_divergence": None}
        return result

    def delete_eval_run(self, er_id: str):
        with self._conn() as c:
            c.execute("DELETE FROM eval_runs WHERE id=?", (er_id,))

    # ── Workflows ─────────────────────────────────────────────────────────────

