"""comparisons repository methods for the Forkmark data layer."""
from __future__ import annotations

from core.store_impl.base import *  # noqa: F401,F403


class ComparisonsMixin:
    def create_comparison(self, run_id: str, workflow_id: str,
                          branch_a_id: str, branch_b_id: str,
                          step_names: list = None,
                          eval_run_id: str = None,
                          test_case_label: str = "",
                          divergence_score: float = None,
                          step_divergence_scores: dict = None,
                          eval_results: dict = None,
                          scoring_status: str = "completed") -> Comparison:
        now = datetime.now(timezone.utc)
        comp = Comparison(
            id=str(uuid.uuid4()), run_id=run_id, workflow_id=workflow_id,
            branch_a_id=branch_a_id, branch_b_id=branch_b_id,
            step_names=step_names or [], created_at=now,
            eval_run_id=eval_run_id, test_case_label=test_case_label,
            divergence_score=divergence_score,
            step_divergence_scores=step_divergence_scores or {},
            eval_results=eval_results or {},
            scoring_status=ScoringStatus(scoring_status),
        )
        with self._conn() as c:
            c.execute("""INSERT INTO comparisons
                (id,run_id,workflow_id,branch_a_id,branch_b_id,step_names,created_at,
                 eval_run_id,test_case_label,divergence_score,step_divergence_scores,
                 eval_results,scoring_status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (comp.id, comp.run_id, comp.workflow_id, comp.branch_a_id,
                 comp.branch_b_id, json.dumps(comp.step_names),
                 comp.created_at.isoformat(), comp.eval_run_id,
                 comp.test_case_label, comp.divergence_score,
                 json.dumps(comp.step_divergence_scores),
                 json.dumps(comp.eval_results), comp.scoring_status.value))
        # ── Flywheel 1: auto-record divergence in performance corpus ──────────
        if comp.test_case_label and comp.eval_run_id:
            try:
                self.record_test_case_performance(
                    test_case_label=comp.test_case_label,
                    workflow_id=workflow_id,
                    eval_run_id=comp.eval_run_id,
                    comparison_id=comp.id,
                    divergence_score=divergence_score,
                )
            except Exception:
                _log.warning("Flywheel: failed to record test-case performance in create_comparison", exc_info=True)
        return comp

    def update_comparison_scoring(self, comp_id: str,
                                  divergence_score: float = None,
                                  step_divergence_scores: dict = None,
                                  eval_results: dict = None,
                                  scoring_status: str = None) -> None:
        """Update scoring results on a comparison (called by background worker)."""
        updates, params = [], []
        if divergence_score is not None:
            updates.append("divergence_score=?"); params.append(divergence_score)
        if step_divergence_scores is not None:
            updates.append("step_divergence_scores=?"); params.append(json.dumps(step_divergence_scores))
        if eval_results is not None:
            updates.append("eval_results=?"); params.append(json.dumps(eval_results))
        if scoring_status is not None:
            updates.append("scoring_status=?"); params.append(scoring_status)
        if not updates:
            return
        params.append(comp_id)
        with self._conn() as c:
            c.execute(f"UPDATE comparisons SET {', '.join(updates)} WHERE id=?", params)

    def list_unscored_comparisons(self, limit: int = 1000) -> List[Comparison]:
        """Comparisons stuck in 'pending'/'running' — used by startup recovery to
        re-enqueue scoring that was interrupted by a crash or restart (the
        in-process background queue is not durable across restarts)."""
        with self._read_conn() as c:
            rows = c.fetchall(
                "SELECT * FROM comparisons "
                "WHERE scoring_status IN ('pending','running') "
                "ORDER BY created_at ASC LIMIT ?",
                (limit,))
        return [Comparison.from_row(_row(r)) for r in rows]

    def get_comparison(self, comp_id: str) -> Optional[Comparison]:
        with self._conn() as c:
            r = c.fetchone("SELECT * FROM comparisons WHERE id=?", (comp_id,))
        return Comparison.from_row(_row(r)) if r else None

    def get_comparison_full(self, comp_id: str) -> Optional[dict]:
        """Fetch comparison + branches + steps + decision + eval_run + run in one
        connection context (6 queries vs the previous 8 round-trips).

        Returns a dict with keys:
            comp, branch_a, branch_b, steps_a, steps_b,
            decision, eval_run_info, run_input
        or None if the comparison doesn't exist.
        """
        with self._conn() as c:
            # 1. Comparison row
            r = c.fetchone("SELECT * FROM comparisons WHERE id=?", (comp_id,))
            if not r:
                return None
            comp = Comparison.from_row(_row(r))

            # 2. Both branches in a single IN query
            branch_rows = c.fetchall(
                "SELECT * FROM branches WHERE id IN (?, ?)",
                (comp.branch_a_id, comp.branch_b_id))
            branches: Dict[str, Branch] = {}
            for br in branch_rows:
                b = Branch.from_row(_row(br))
                branches[b.id] = b

            # 3. All step outputs for both branches in one query
            step_rows = c.fetchall(
                "SELECT * FROM step_outputs "
                "WHERE branch_id IN (?, ?) "
                "ORDER BY branch_id, step_index",
                (comp.branch_a_id, comp.branch_b_id))
            steps_by_branch: Dict[str, List[StepOutput]] = {
                comp.branch_a_id: [], comp.branch_b_id: []}
            for sr in step_rows:
                so = StepOutput.from_row(_row(sr))
                steps_by_branch.setdefault(so.branch_id, []).append(so)

            # 4. Decision (optional)
            decision_obj = None
            if comp.decision_id:
                dr = c.fetchone("SELECT * FROM decisions WHERE id=?",
                                (comp.decision_id,))
                if dr:
                    decision_obj = Decision.from_row(_row(dr))

            # 5. Eval run — only id + name needed by the response
            eval_run_info = None
            if comp.eval_run_id:
                er = c.fetchone("SELECT id, name FROM eval_runs WHERE id=?",
                                (comp.eval_run_id,))
                if er:
                    r2 = _row(er)
                    eval_run_info = {"id": r2["id"], "name": r2["name"]}

            # 6. Run input data
            run_input: dict = {}
            rr = c.fetchone("SELECT * FROM workflow_runs WHERE id=?", (comp.run_id,))
            if rr:
                run_input = WorkflowRun.from_row(_row(rr)).input_data

        return {
            "comp":          comp,
            "branch_a":      branches.get(comp.branch_a_id),
            "branch_b":      branches.get(comp.branch_b_id),
            "steps_a":       steps_by_branch.get(comp.branch_a_id, []),
            "steps_b":       steps_by_branch.get(comp.branch_b_id, []),
            "decision":      decision_obj,
            "eval_run_info": eval_run_info,
            "run_input":     run_input,
        }

    def list_comparisons(self, workflow_id: str = None, undecided_only: bool = False,
                         eval_run_id: str = None, run_id: str = None,
                         limit: int = 200, offset: int = 0, cursor: str = None) -> List[Comparison]:
        filters, params = [], []
        if workflow_id:
            filters.append("workflow_id=?"); params.append(workflow_id)
        if undecided_only:
            filters.append("decided=0")
        if eval_run_id:
            filters.append("eval_run_id=?"); params.append(eval_run_id)
        if run_id:
            filters.append("run_id=?"); params.append(run_id)
        if cursor:
            filters.append("created_at < ?"); params.append(cursor)
        
        where = ("WHERE " + " AND ".join(filters)) if filters else ""
        order = "ORDER BY COALESCE(divergence_score, -1) DESC, created_at DESC"
        with self._conn() as c:
            rows = c.fetchall(
                f"SELECT * FROM comparisons {where} {order} LIMIT ? OFFSET ?",
                params + [limit, offset])
        return [Comparison.from_row(_row(r)) for r in rows]

    def mark_comparison_decided(self, comp_id: str, decision_id: str):
        with self._conn() as c:
            c.execute("UPDATE comparisons SET decided=1, decision_id=? WHERE id=?",
                      (decision_id, comp_id))

    # ── Decisions ─────────────────────────────────────────────────────────────

