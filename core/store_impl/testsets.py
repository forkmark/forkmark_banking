"""testsets repository methods for the Forkmark data layer."""
from __future__ import annotations

from core.store_impl.base import *  # noqa: F401,F403


class TestSetsMixin:
    def create_test_set(self, name: str, description: str = "",
                        workflow_id: str = None) -> TestSet:
        now = datetime.now(timezone.utc)
        ts = TestSet(
            id=str(uuid.uuid4()), name=name, description=description,
            workflow_id=workflow_id, created_at=now,
        )
        with self._conn() as c:
            c.execute(
                "INSERT INTO test_sets (id,name,description,workflow_id,created_at) VALUES (?,?,?,?,?)",
                (ts.id, ts.name, ts.description, ts.workflow_id, ts.created_at.isoformat()),
            )
        return ts

    def get_test_set(self, ts_id: str) -> Optional[TestSet]:
        with self._conn() as c:
            r = c.fetchone("""
                SELECT ts.*, COUNT(tc.id) AS case_count
                FROM test_sets ts
                LEFT JOIN test_cases tc ON tc.test_set_id = ts.id
                WHERE ts.id = ?
                GROUP BY ts.id
            """, (ts_id,))
        return TestSet.from_row(_row(r)) if r else None

    def list_test_sets(self, workflow_id: str = None) -> List[TestSet]:
        """Returns test sets with case_count populated via a single JOIN query."""
        with self._conn() as c:
            if workflow_id:
                rows = c.fetchall("""
                    SELECT ts.*, COUNT(tc.id) AS case_count
                    FROM test_sets ts
                    LEFT JOIN test_cases tc ON tc.test_set_id = ts.id
                    WHERE ts.workflow_id = ?
                    GROUP BY ts.id
                    ORDER BY ts.created_at DESC
                """, (workflow_id,))
            else:
                rows = c.fetchall("""
                    SELECT ts.*, COUNT(tc.id) AS case_count
                    FROM test_sets ts
                    LEFT JOIN test_cases tc ON tc.test_set_id = ts.id
                    GROUP BY ts.id
                    ORDER BY ts.created_at DESC
                """)
        return [TestSet.from_row(_row(r)) for r in rows]

    def delete_test_set(self, ts_id: str):
        with self._conn() as c:
            c.execute("DELETE FROM test_sets WHERE id=?", (ts_id,))

    # ── TestCases ─────────────────────────────────────────────────────────────

    def add_test_case(self, test_set_id: str, label: str,
                      input_data: dict = None, tags: list = None,
                      expected_output: str = None) -> TestCase:
        # Block mutations on frozen test sets
        ts = self.get_test_set(test_set_id)
        if ts and ts.is_frozen:
            raise ValueError(
                f"Test set '{ts.name}' is frozen (linked to an eval run). "
                f"Create a new version to add cases."
            )
        now = datetime.now(timezone.utc)
        tc = TestCase(
            id=str(uuid.uuid4()), test_set_id=test_set_id, label=label,
            input_data=input_data or {}, tags=tags or [], created_at=now,
            expected_output=expected_output,
        )
        with self._conn() as c:
            c.execute(
                "INSERT INTO test_cases (id,test_set_id,label,input_data,expected_output,tags,created_at) VALUES (?,?,?,?,?,?,?)",
                (tc.id, tc.test_set_id, tc.label,
                 json.dumps(tc.input_data), tc.expected_output, json.dumps(tc.tags), tc.created_at.isoformat()),
            )
        return tc

    def list_test_cases(self, test_set_id: str) -> List[TestCase]:
        with self._conn() as c:
            rows = c.fetchall(
                "SELECT * FROM test_cases WHERE test_set_id=? ORDER BY created_at", (test_set_id,))
        return [TestCase.from_row(_row(r)) for r in rows]

    def delete_test_case(self, tc_id: str, test_set_id: str = None):
        """Delete a test case.

        If test_set_id is provided, the delete is scoped to that test set —
        the row is only removed if it belongs to that set (prevents IDOR).
        Raises ValueError if the test set is frozen.
        """
        if test_set_id:
            ts = self.get_test_set(test_set_id)
            if ts and ts.is_frozen:
                raise ValueError(
                    f"Test set '{ts.name}' is frozen (linked to an eval run). "
                    f"Create a new version to modify cases."
                )
        with self._conn() as c:
            if test_set_id:
                c.execute("DELETE FROM test_cases WHERE id=? AND test_set_id=?",
                          (tc_id, test_set_id))
            else:
                c.execute("DELETE FROM test_cases WHERE id=?", (tc_id,))

    def freeze_test_set(self, ts_id: str) -> None:
        """Mark a test set as frozen — prevents future mutations."""
        with self._conn() as c:
            c.execute("UPDATE test_sets SET is_frozen=1 WHERE id=?", (ts_id,))

    def create_test_set_version(self, ts_id: str) -> TestSet:
        """Create a new mutable copy of a frozen test set with incremented version.

        Copies all test cases from the original set into the new one.
        """
        original = self.get_test_set(ts_id)
        if not original:
            raise ValueError(f"Test set {ts_id} not found")

        now = datetime.now(timezone.utc)
        new_ts = TestSet(
            id=str(uuid.uuid4()), name=original.name,
            description=original.description, workflow_id=original.workflow_id,
            created_at=now, version=original.version + 1, is_frozen=False,
        )
        with self._conn() as c:
            c.execute(
                "INSERT INTO test_sets (id,name,description,workflow_id,created_at,version,is_frozen) "
                "VALUES (?,?,?,?,?,?,?)",
                (new_ts.id, new_ts.name, new_ts.description, new_ts.workflow_id,
                 new_ts.created_at.isoformat(), new_ts.version, 0),
            )
            # Copy test cases (including expected_output)
            cases = self.list_test_cases(ts_id)
            for tc in cases:
                c.execute(
                    "INSERT INTO test_cases (id,test_set_id,label,input_data,expected_output,tags,created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), new_ts.id, tc.label,
                     json.dumps(tc.input_data), tc.expected_output, json.dumps(tc.tags),
                     now.isoformat()),
                )
        new_ts.case_count = len(cases)
        return new_ts

    def bulk_add_test_cases(self, test_set_id: str,
                            cases: List[Dict[str, Any]]) -> List[TestCase]:
        """Insert multiple test cases at once (single transaction)."""
        result = []
        now = datetime.now(timezone.utc)
        rows_to_insert = []
        for item in cases:
            tc = TestCase(
                id=str(uuid.uuid4()), test_set_id=test_set_id,
                label=item.get("label", f"case-{len(result)+1}"),
                input_data=item.get("input_data", {k: v for k, v in item.items()
                                                   if k not in ("label", "tags", "input_data", "expected_output")}),
                tags=item.get("tags", []), created_at=now,
                expected_output=item.get("expected_output"),
            )
            rows_to_insert.append(
                (tc.id, tc.test_set_id, tc.label,
                 json.dumps(tc.input_data), tc.expected_output, json.dumps(tc.tags), tc.created_at.isoformat())
            )
            result.append(tc)
        with self._conn() as c:
            c.executemany(
                "INSERT INTO test_cases (id,test_set_id,label,input_data,expected_output,tags,created_at) VALUES (?,?,?,?,?,?,?)",
                rows_to_insert,
            )
        return result

    # ── EvalRuns ──────────────────────────────────────────────────────────────

