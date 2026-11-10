"""Tests verifying migrations v16 through v19 are idempotent and schema intact."""
from __future__ import annotations

from pathlib import Path
import pytest

from core.store import Database


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "migration_test.db"


def test_migrations_v16_v19_schema_created(db_path: Path):
    """Verify all new tables and columns created by migrations v16 through v19."""
    db = Database(str(db_path))

    with db._read_conn() as c:
        # Check v16: gateway_call_log table
        row = c.fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name='gateway_call_log'")
        assert row is not None

        # Check v17: llm_providers columns
        rows = c.fetchall("PRAGMA table_info(llm_providers)")
        cols = {row["name"] for row in rows}
        for col in [
            "deployment_kind", "model_family", "default_model", "azure_deployment",
            "api_version", "azure_deployment_type", "inference_region", "hosting",
            "vendor_name", "licence", "max_concurrency", "timeout_s", "rpm_limit",
            "rpd_limit", "max_input_tokens"
        ]:
            assert col in cols, f"Missing column {col} in llm_providers"

        # Check v17: model_inventory.connector_id
        rows = c.fetchall("PRAGMA table_info(model_inventory)")
        cols = {row["name"] for row in rows}
        assert "connector_id" in cols

        # Check v17: eval_runs.run_kind
        rows = c.fetchall("PRAGMA table_info(eval_runs)")
        cols = {row["name"] for row in rows}
        assert "run_kind" in cols

        # Check v17: step_outputs.provider_meta
        rows = c.fetchall("PRAGMA table_info(step_outputs)")
        cols = {row["name"] for row in rows}
        assert "provider_meta" in cols

        # Check v17: model_fingerprints table
        row = c.fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name='model_fingerprints'")
        assert row is not None

        # Check v18: safety_results table
        row = c.fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name='safety_results'")
        assert row is not None

        # Check v19: monitors and monitor_runs tables
        row = c.fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name='monitors'")
        assert row is not None
        row = c.fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name='monitor_runs'")
        assert row is not None


def test_migrations_are_idempotent(db_path: Path):
    """Re-running migrations / re-opening Database must succeed without errors and preserve data."""
    db1 = Database(str(db_path))
    # Insert test data into gateway_call_log and monitors
    call_log = db1.log_gateway_call(
        purpose="eval_run",
        host="api.openai.com",
        decision="allowed",
        model="gpt-4o",
        http_status=200,
        latency_ms=120,
    )
    assert isinstance(call_log, str) and call_log != ""

    monitor = db1.create_monitor({
        "governed_model_id": "test-model",
        "connector_id": "conn-1",
        "test_set_id": "ts-1",
        "interval_hours": 24,
    })
    assert monitor["id"] is not None

    # Re-initialize database on the same file (simulating application restart)
    db2 = Database(str(db_path))
    fetched_monitor = db2.get_monitor(monitor["id"])
    assert fetched_monitor is not None
    assert fetched_monitor["governed_model_id"] == "test-model"

    logs = db2.list_gateway_calls(limit=10)
    assert len(logs) >= 1
    assert logs[0]["model"] == "gpt-4o"
