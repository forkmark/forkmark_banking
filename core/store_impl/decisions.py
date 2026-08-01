"""decisions repository methods for the Forkmark data layer."""
from __future__ import annotations

from core.store_impl.base import *  # noqa: F401,F403


class DecisionsMixin:
    def create_decision(self, comparison_id: str, run_id: str, workflow_id: str,
                        reviewer_id: str, choice: DecisionChoice,
                        confidence: ConfidenceLevel, rationale_for_choice: str,
                        rationale_for_rejection: str, tags: list = None,
                        branch_winner_id: str = None, branch_loser_id: str = None,
                        divergence_score: float = 0.0,
                        divergence_summary: str = None,
                        eval_run_id: str = None) -> Decision:
        import hashlib
        now = datetime.now(timezone.utc)
        d = Decision(
            id=str(uuid.uuid4()), comparison_id=comparison_id, run_id=run_id,
            workflow_id=workflow_id, reviewer_id=reviewer_id, choice=choice,
            confidence=confidence, rationale_for_choice=rationale_for_choice,
            rationale_for_rejection=rationale_for_rejection, tags=tags or [],
            created_at=now, branch_winner_id=branch_winner_id,
            branch_loser_id=branch_loser_id, divergence_score=divergence_score,
            divergence_summary=divergence_summary, eval_run_id=eval_run_id,
        )
        _comp_row = None
        with self._conn() as c:
            c.execute("""INSERT INTO decisions
                (id,comparison_id,run_id,workflow_id,reviewer_id,choice,confidence,
                 rationale_for_choice,rationale_for_rejection,tags,created_at,
                 branch_winner_id,branch_loser_id,divergence_score,divergence_summary,
                 eval_run_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (d.id, d.comparison_id, d.run_id, d.workflow_id, d.reviewer_id,
                 d.choice.value, d.confidence.value, d.rationale_for_choice,
                 d.rationale_for_rejection, json.dumps(d.tags),
                 d.created_at.isoformat(), d.branch_winner_id, d.branch_loser_id,
                 d.divergence_score, d.divergence_summary, d.eval_run_id))
            c.execute("UPDATE comparisons SET decided=1, decision_id=? WHERE id=?",
                      (d.id, comparison_id))
            c.execute("UPDATE workflows SET decision_count=decision_count+1 WHERE id=?",
                      (workflow_id,))

            # ── Flywheel 2: compute provenance_hash + data_category ───────────
            run_row = c.fetchone(
                "SELECT input_data FROM workflow_runs WHERE id=?", (run_id,))
            raw_input = ""
            if run_row:
                try:
                    _rr = _row(run_row)
                    raw_input = json.loads(_rr.get("input_data") or "{}").get("input", "") or ""
                except Exception:
                    _rr = _row(run_row)
                    raw_input = str(_rr.get("input_data") or "")
            ph = hashlib.sha256(
                f"{workflow_id}:{d.id}:{str(raw_input)[:200]}".encode()
            ).hexdigest()
            # auto-classify data_category from tags
            tag_str = " ".join(tags or []).lower()
            if any(w in tag_str for w in ("legal", "lawsuit", "attorney", "injury", "cpsc")):
                dc = "legal"
            elif any(w in tag_str for w in ("billing", "charge", "refund", "payment")):
                dc = "billing"
            elif any(w in tag_str for w in ("safety", "allergy", "medical", "health")):
                dc = "safety"
            elif any(w in tag_str for w in ("churn", "retention", "vip", "escalation")):
                dc = "retention"
            else:
                dc = "general"
            c.execute(
                "UPDATE decisions SET provenance_hash=?, data_category=? WHERE id=?",
                (ph, dc, d.id),
            )

            # grab test_case_label + eval_run_id from comparison for flywheel 1
            _comp_row = c.fetchone(
                "SELECT test_case_label, eval_run_id FROM comparisons WHERE id=?",
                (comparison_id,),
            )

        # ── Flywheel 1: record human choice in performance corpus ─────────────
        if _comp_row:
            _cr = _row(_comp_row)
            tcl  = _cr.get("test_case_label") or ""
            erid = _cr.get("eval_run_id") or eval_run_id or ""
            if tcl and erid:
                try:
                    self.record_test_case_performance(
                        test_case_label=tcl,
                        workflow_id=workflow_id,
                        eval_run_id=erid,
                        decision_choice=choice.value,
                        reviewer_confidence=confidence.value,
                    )
                except Exception:
                    _log.warning("Flywheel: failed to record performance in create_decision", exc_info=True)
        return d

    def update_decision(self, decision_id: str, choice: DecisionChoice,
                        confidence: ConfidenceLevel, rationale_for_choice: str,
                        rationale_for_rejection: str, tags: list,
                        reviewer_id: str = None,
                        branch_winner_id: str = None,
                        branch_loser_id: str = None) -> Optional[Decision]:
        """Update an existing decision in place (edit flow).

        Also updates branch_winner_id/branch_loser_id and sets updated_at so the
        edit is auditable — previously these fields went stale after an edit.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            if reviewer_id:
                c.execute("""
                    UPDATE decisions
                    SET choice=?, confidence=?, rationale_for_choice=?,
                        rationale_for_rejection=?, tags=?, reviewer_id=?,
                        branch_winner_id=?, branch_loser_id=?, updated_at=?
                    WHERE id=?
                """, (choice.value, confidence.value, rationale_for_choice,
                      rationale_for_rejection, json.dumps(tags), reviewer_id,
                      branch_winner_id, branch_loser_id, now, decision_id))
            else:
                c.execute("""
                    UPDATE decisions
                    SET choice=?, confidence=?, rationale_for_choice=?,
                        rationale_for_rejection=?, tags=?,
                        branch_winner_id=?, branch_loser_id=?, updated_at=?
                    WHERE id=?
                """, (choice.value, confidence.value, rationale_for_choice,
                      rationale_for_rejection, json.dumps(tags),
                      branch_winner_id, branch_loser_id, now, decision_id))
            r = c.fetchone("SELECT * FROM decisions WHERE id=?", (decision_id,))
        return Decision.from_row(_row(r)) if r else None

    def get_decision(self, decision_id: str) -> Optional[Decision]:
        with self._conn() as c:
            r = c.fetchone("SELECT * FROM decisions WHERE id=?", (decision_id,))
        return Decision.from_row(_row(r)) if r else None

    def list_decisions(self, workflow_id: str = None, eval_run_id: str = None,
                       limit: int = 100, offset: int = 0) -> List[Decision]:
        filters, params = [], []
        if workflow_id:
            filters.append("workflow_id=?"); params.append(workflow_id)
        if eval_run_id:
            filters.append("eval_run_id=?"); params.append(eval_run_id)
        where = ("WHERE " + " AND ".join(filters)) if filters else ""
        with self._conn() as c:
            rows = c.fetchall(
                f"SELECT * FROM decisions {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params + [limit, offset])
        return [Decision.from_row(_row(r)) for r in rows]

    def list_decision_tags(self, workflow_id: str = None) -> List[str]:
        """Return distinct tags used across decisions, sorted by frequency."""
        filters = ["tags IS NOT NULL", "tags != '[]'"]
        params: List = []
        if workflow_id:
            filters.append("workflow_id=?")
            params.append(workflow_id)
        where = "WHERE " + " AND ".join(filters)
        with self._conn() as c:
            rows = c.fetchall(f"SELECT tags FROM decisions {where}", params)
        freq: Dict[str, int] = {}
        for row in rows:
            r = _row(row)
            try:
                tags = json.loads(r.get("tags") or "[]")
            except Exception:
                continue
            for t in tags:
                if isinstance(t, str) and t.strip():
                    freq[t.strip()] = freq.get(t.strip(), 0) + 1
        return sorted(freq.keys(), key=lambda t: -freq[t])

    def export_decisions_jsonl(self, workflow_id: str = None,
                               eval_run_id: str = None):
        filters, params = [], []
        if workflow_id:
            filters.append("workflow_id=?"); params.append(workflow_id)
        if eval_run_id:
            filters.append("eval_run_id=?"); params.append(eval_run_id)
        where = ("WHERE " + " AND ".join(filters)) if filters else ""
        
        with self._read_conn() as c:
            cur = c.execute(f"SELECT * FROM decisions {where} ORDER BY created_at DESC", params)
            for row in cur:
                d = Decision.from_row(_row(row))
                yield json.dumps({
                    "comparison_id":          d.comparison_id,
                    "eval_run_id":            d.eval_run_id,
                    "choice":                 d.choice.value,
                    "confidence":             d.confidence.value,
                    "rationale_for_choice":   d.rationale_for_choice,
                    "rationale_for_rejection":d.rationale_for_rejection,
                    "tags":                   d.tags,
                    "divergence_score":       d.divergence_score,
                    "created_at":             d.created_at.isoformat(),
                }) + "\n"

    # ── API Keys ──────────────────────────────────────────────────────────────

