"""stats repository methods for the Forkmark data layer."""
from __future__ import annotations

from core.store_impl.base import *  # noqa: F401,F403


class StatsMixin:
    def get_stats(self) -> dict:
        """Dashboard stats computed entirely in SQL — single query for all counts."""
        with self._read_conn() as c:
            # Combine 6 COUNT queries into one cross-join (single table scan each)
            stats_row = _row(c.fetchone("""
                SELECT
                    (SELECT COUNT(*) FROM workflows)      AS wf_count,
                    (SELECT COUNT(*) FROM workflow_runs)   AS run_count,
                    (SELECT COUNT(*) FROM decisions)       AS dec_count,
                    (SELECT COUNT(*) FROM eval_runs)       AS er_count,
                    (SELECT COUNT(*) FROM comparisons WHERE decided=0) AS pending,
                    (SELECT COUNT(*) FROM eval_runs
                     WHERE status IN ('pending','running')) AS active_er
            """))
            wf_count  = stats_row["wf_count"]
            run_count = stats_row["run_count"]
            dec_count = stats_row["dec_count"]
            er_count  = stats_row["er_count"]
            pending   = stats_row["pending"]
            active_er = stats_row["active_er"]

            choice_rows = c.fetchall(
                "SELECT choice, COUNT(*) AS n FROM decisions GROUP BY choice")
            conf_rows = c.fetchall(
                "SELECT confidence, COUNT(*) AS n FROM decisions GROUP BY confidence")

        choice_breakdown = {"A": 0, "B": 0, "neither": 0, "both": 0}
        for r in choice_rows:
            r = _row(r)
            choice_breakdown[r["choice"]] = r["n"]

        conf_breakdown = {"high": 0, "medium": 0, "low": 0}
        for r in conf_rows:
            r = _row(r)
            conf_breakdown[r["confidence"]] = r["n"]

        return {
            "total_workflows":      wf_count,
            "total_runs":           run_count,
            "total_decisions":      dec_count,
            "total_eval_runs":      er_count,
            "pending_review":       pending,
            "active_eval_runs":     active_er,
            "choice_breakdown":     choice_breakdown,
            "confidence_breakdown": conf_breakdown,
        }

    # ── Cost Aggregation ──────────────────────────────────────────────────────

    def get_cost_breakdown(self, run_id: str = None,
                           comparison_id: str = None,
                           eval_run_id: str = None) -> dict:
        """Compute per-branch and total cost breakdown from step_outputs.

        Returns:
            {
                "total_cost_usd": float,
                "total_tokens_input": int,
                "total_tokens_output": int,
                "branches": [
                    {"branch_id": str, "branch_name": str, "model_id": str,
                     "cost_usd": float, "tokens_input": int, "tokens_output": int,
                     "step_count": int},
                    ...
                ]
            }
        """
        with self._conn() as c:
            if comparison_id:
                # Get branch IDs from comparison
                comp = c.fetchone(
                    "SELECT branch_a_id, branch_b_id FROM comparisons WHERE id=?",
                    (comparison_id,),
                )
                if not comp:
                    return {"total_cost_usd": 0, "total_tokens_input": 0,
                            "total_tokens_output": 0, "branches": []}
                comp = _row(comp)
                branch_ids = (comp["branch_a_id"], comp["branch_b_id"])
                rows = c.fetchall("""
                    SELECT s.branch_id,
                           b.name AS branch_name,
                           s.model_id,
                           COALESCE(SUM(s.cost_usd), 0) AS cost_usd,
                           COALESCE(SUM(s.tokens_input), 0) AS tokens_input,
                           COALESCE(SUM(s.tokens_output), 0) AS tokens_output,
                           COUNT(*) AS step_count
                    FROM step_outputs s
                    JOIN branches b ON b.id = s.branch_id
                    WHERE s.branch_id IN (?, ?)
                    GROUP BY s.branch_id, s.model_id
                    ORDER BY cost_usd DESC
                """, branch_ids)
            elif eval_run_id:
                rows = c.fetchall("""
                    SELECT s.branch_id,
                           b.name AS branch_name,
                           s.model_id,
                           COALESCE(SUM(s.cost_usd), 0) AS cost_usd,
                           COALESCE(SUM(s.tokens_input), 0) AS tokens_input,
                           COALESCE(SUM(s.tokens_output), 0) AS tokens_output,
                           COUNT(*) AS step_count
                    FROM step_outputs s
                    JOIN branches b ON b.id = s.branch_id
                    JOIN workflow_runs wr ON wr.id = s.run_id
                    WHERE wr.eval_run_id = ?
                    GROUP BY s.branch_id, s.model_id
                    ORDER BY cost_usd DESC
                """, (eval_run_id,))
            elif run_id:
                rows = c.fetchall("""
                    SELECT s.branch_id,
                           b.name AS branch_name,
                           s.model_id,
                           COALESCE(SUM(s.cost_usd), 0) AS cost_usd,
                           COALESCE(SUM(s.tokens_input), 0) AS tokens_input,
                           COALESCE(SUM(s.tokens_output), 0) AS tokens_output,
                           COUNT(*) AS step_count
                    FROM step_outputs s
                    JOIN branches b ON b.id = s.branch_id
                    WHERE s.run_id = ?
                    GROUP BY s.branch_id, s.model_id
                    ORDER BY cost_usd DESC
                """, (run_id,))
            else:
                return {"total_cost_usd": 0, "total_tokens_input": 0,
                        "total_tokens_output": 0, "branches": []}

        branches = []
        total_cost = 0.0
        total_in = 0
        total_out = 0
        for r in rows:
            r = _row(r)
            branches.append({
                "branch_id":     r["branch_id"],
                "branch_name":   r["branch_name"],
                "model_id":      r["model_id"],
                "cost_usd":      float(r["cost_usd"]),
                "tokens_input":  int(r["tokens_input"]),
                "tokens_output": int(r["tokens_output"]),
                "step_count":    int(r["step_count"]),
            })
            total_cost += float(r["cost_usd"])
            total_in   += int(r["tokens_input"])
            total_out  += int(r["tokens_output"])

        return {
            "total_cost_usd":      round(total_cost, 6),
            "total_tokens_input":  total_in,
            "total_tokens_output": total_out,
            "branches":            branches,
        }

    # ── TestSets ──────────────────────────────────────────────────────────────

