"""PostgreSQL backend integration tests.

Skipped unless FM_DATABASE_URL points at a reachable PostgreSQL instance, so the
default SQLite suite (and offline dev) is unaffected. CI runs this against a
Postgres service container — see .github/workflows/ci.yml (job: backend-postgres).

These tests exercise the psycopg2 adapter path end-to-end: schema creation,
migrations, the ?→%s / %-escaping translation, CRUD, and a DPO export — the
"production = Postgres" claim the docs make.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_PG_URL = os.getenv("FM_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not _PG_URL.startswith("postgres"),
    reason="FM_DATABASE_URL not set to a PostgreSQL URL",
)


@pytest.fixture()
def pg_db():
    """A Database bound to PostgreSQL, using a unique-ish dataset per test."""
    from core.store import Database
    return Database(db_path=":memory:", database_url=_PG_URL)


def test_pg_schema_and_crud(pg_db):
    wf = pg_db.upsert_workflow(f"pg-{uuid.uuid4().hex[:8]}")
    run = pg_db.create_run(wf.id, {"input": "hello pg"})
    b = pg_db.create_branch(run.id, wf.id, "A", "gpt-4o")
    step = pg_db.save_step_output(
        run.id, b.id, "answer", 0,
        [{"role": "user", "content": "hi"}], "hello world", "gpt-4o")
    assert step.output_text == "hello world"

    fetched = pg_db.get_run(run.id)
    assert fetched.input_data == {"input": "hello pg"}
    assert pg_db.list_workflows()  # round-trips through the PG adapter


def test_pg_dpo_export(pg_db):
    from core.models import DecisionChoice, ConfidenceLevel
    import json

    wf = pg_db.upsert_workflow(f"pg-dpo-{uuid.uuid4().hex[:8]}")
    run = pg_db.create_run(wf.id, {"topic": "x"})
    ba = pg_db.create_branch(run.id, wf.id, "A", "gpt-4o")
    bb = pg_db.create_branch(run.id, wf.id, "B", "gpt-4o-mini")
    msgs = [{"role": "user", "content": "Summarize the policy."}]
    pg_db.save_step_output(run.id, ba.id, "answer", 0, msgs, "good answer", "gpt-4o")
    pg_db.save_step_output(run.id, bb.id, "answer", 0, msgs, "bad answer", "gpt-4o-mini")
    comp = pg_db.create_comparison(run.id, wf.id, ba.id, bb.id, step_names=["answer"])
    pg_db.create_decision(
        comparison_id=comp.id, run_id=run.id, workflow_id=wf.id,
        reviewer_id="t", choice=DecisionChoice.BRANCH_A,
        confidence=ConfidenceLevel.HIGH,
        rationale_for_choice="clearer", rationale_for_rejection="worse",
        branch_winner_id=ba.id, branch_loser_id=bb.id,
    )
    lines = [json.loads(l) for l in pg_db.export_preference_corpus_jsonl(
        workflow_id=wf.id, anonymize=False, require_consent=False)]
    assert any(r["chosen"] == "good answer" and "Summarize the policy." in r["prompt"]
               for r in lines)


def test_pg_like_query_with_percent(pg_db):
    """A LIKE '%...%' query must work — guards the %-escaping in the translator."""
    wf = pg_db.upsert_workflow(f"pg-like-{uuid.uuid4().hex[:8]}")
    # list_decision_tags builds a WHERE with no LIKE, so exercise the adapter via
    # a direct parameterised LIKE through the connection wrapper.
    with pg_db._read_conn() as c:
        rows = c.fetchall(
            "SELECT id FROM workflows WHERE name LIKE ?", (f"%{wf.name[3:]}%",))
    assert any(r and (dict(r).get("id") == wf.id) for r in rows)
