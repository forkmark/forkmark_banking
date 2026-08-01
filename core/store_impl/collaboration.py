"""collaboration repository methods for the Forkmark data layer."""
from __future__ import annotations

from core.store_impl.base import *  # noqa: F401,F403


class CollaborationMixin:
    def add_comment(self, comparison_id: str, author_id: str, body: str,
                    author_name: str = "", parent_id: str = None) -> dict:
        """Add a comment to a comparison. Supports threading via parent_id."""
        cid = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            c.execute(
                """INSERT INTO comments
                   (id, comparison_id, author_id, author_name, body, parent_id,
                    created_at, updated_at, is_resolved)
                   VALUES (?,?,?,?,?,?,?,?,0)""",
                (cid, comparison_id, author_id, author_name, body, parent_id,
                 now, now),
            )
        return self.get_comment(cid)

    def get_comment(self, comment_id: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.fetchone("SELECT * FROM comments WHERE id=?", (comment_id,))
        return _row(row) if row else None

    def list_comments(self, comparison_id: str) -> List[dict]:
        """List all comments for a comparison, ordered chronologically."""
        with self._conn() as c:
            rows = c.fetchall(
                "SELECT * FROM comments WHERE comparison_id=? ORDER BY created_at ASC",
                (comparison_id,),
            )
        return [_row(r) for r in rows]

    def update_comment(self, comment_id: str, body: str = None,
                       is_resolved: bool = None) -> Optional[dict]:
        """Update a comment body or resolve status."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            if body is not None:
                c.execute("UPDATE comments SET body=?, updated_at=? WHERE id=?",
                          (body, now, comment_id))
            if is_resolved is not None:
                c.execute("UPDATE comments SET is_resolved=?, updated_at=? WHERE id=?",
                          (1 if is_resolved else 0, now, comment_id))
        return self.get_comment(comment_id)

    def delete_comment(self, comment_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM comments WHERE id=?", (comment_id,))

    # ── Collaboration: review assignments ───────────────────────────────────

    def assign_review(self, eval_run_id: str, comparison_id: str,
                      reviewer_id: str, assigned_by: str = "",
                      notes: str = "") -> dict:
        """Assign a comparison to a reviewer."""
        aid = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            c.execute(
                """INSERT INTO review_assignments
                   (id, eval_run_id, comparison_id, reviewer_id, assigned_by,
                    status, assigned_at, notes)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (aid, eval_run_id, comparison_id, reviewer_id, assigned_by,
                 "pending", now, notes),
            )
            # Update comparison status
            c.execute(
                "UPDATE comparisons SET review_status='assigned', assigned_to=? WHERE id=?",
                (reviewer_id, comparison_id),
            )
        return self.get_assignment(aid)

    def get_assignment(self, assignment_id: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.fetchone("SELECT * FROM review_assignments WHERE id=?",
                             (assignment_id,))
        return _row(row) if row else None

    def list_assignments(self, eval_run_id: str = None,
                         reviewer_id: str = None,
                         status: str = None) -> List[dict]:
        """List review assignments with optional filters."""
        conditions, params = [], []
        if eval_run_id:
            conditions.append("eval_run_id=?"); params.append(eval_run_id)
        if reviewer_id:
            conditions.append("reviewer_id=?"); params.append(reviewer_id)
        if status:
            conditions.append("status=?"); params.append(status)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        with self._conn() as c:
            rows = c.fetchall(
                f"SELECT * FROM review_assignments {where} ORDER BY assigned_at DESC",
                params,
            )
        return [_row(r) for r in rows]

    def update_assignment_status(self, assignment_id: str, status: str,
                                 notes: str = None) -> Optional[dict]:
        """Update assignment status: pending → in_review → completed / skipped."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            updates = ["status=?"]
            params = [status]
            if status in ("completed", "skipped"):
                updates.append("completed_at=?")
                params.append(now)
            if notes is not None:
                updates.append("notes=?")
                params.append(notes)
            params.append(assignment_id)
            c.execute(
                f"UPDATE review_assignments SET {', '.join(updates)} WHERE id=?",
                params,
            )
            # Sync comparison review_status (inline read to avoid nested lock)
            row = c.fetchone("SELECT * FROM review_assignments WHERE id=?",
                             (assignment_id,))
            if row:
                comp_id = _row(row)["comparison_id"]
                comp_status = "reviewed" if status == "completed" else status
                c.execute(
                    "UPDATE comparisons SET review_status=? WHERE id=?",
                    (comp_status, comp_id),
                )
        return self.get_assignment(assignment_id)

    def bulk_assign_reviews(self, eval_run_id: str, reviewer_ids: List[str],
                            assigned_by: str = "") -> List[dict]:
        """Round-robin assign all unassigned comparisons in an eval run to reviewers."""
        with self._conn() as c:
            rows = c.fetchall(
                "SELECT id FROM comparisons WHERE eval_run_id=? AND review_status='pending'",
                (eval_run_id,),
            )
        comp_ids = [_row(r)["id"] for r in rows]
        if not comp_ids or not reviewer_ids:
            return []

        assignments = []
        for i, comp_id in enumerate(comp_ids):
            reviewer = reviewer_ids[i % len(reviewer_ids)]
            a = self.assign_review(eval_run_id, comp_id, reviewer, assigned_by)
            if a:
                assignments.append(a)
        return assignments

    def get_review_queue(self, reviewer_id: str) -> List[dict]:
        """Get a reviewer's pending queue with comparison details."""
        with self._conn() as c:
            rows = c.fetchall(
                """SELECT ra.*, c.divergence_score, c.eval_run_id AS comp_eval_run_id
                   FROM review_assignments ra
                   JOIN comparisons c ON c.id = ra.comparison_id
                   WHERE ra.reviewer_id=? AND ra.status IN ('pending', 'in_review')
                   ORDER BY c.divergence_score DESC""",
                (reviewer_id,),
            )
        return [_row(r) for r in rows]

    def get_review_stats(self, eval_run_id: str) -> dict:
        """Get review progress stats for an eval run."""
        with self._conn() as c:
            total = c.fetchone(
                "SELECT COUNT(*) as cnt FROM comparisons WHERE eval_run_id=?",
                (eval_run_id,),
            )
            reviewed = c.fetchone(
                "SELECT COUNT(*) as cnt FROM comparisons WHERE eval_run_id=? AND review_status='reviewed'",
                (eval_run_id,),
            )
            assigned = c.fetchone(
                "SELECT COUNT(*) as cnt FROM comparisons WHERE eval_run_id=? AND review_status='assigned'",
                (eval_run_id,),
            )
            pending = c.fetchone(
                "SELECT COUNT(*) as cnt FROM comparisons WHERE eval_run_id=? AND review_status='pending'",
                (eval_run_id,),
            )
        return {
            "total": _row(total)["cnt"] if total else 0,
            "reviewed": _row(reviewed)["cnt"] if reviewed else 0,
            "assigned": _row(assigned)["cnt"] if assigned else 0,
            "pending": _row(pending)["cnt"] if pending else 0,
        }

    # ── Agent comparison CRUD ────────────────────────────────────────────────

