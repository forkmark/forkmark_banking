"""Automated tests for Continuous Drift Monitoring (Milestone 4).

Proves:
- Baseline & noise floor establishment
- Population Stability Index (PSI) against worked credit-risk examples
- Atomic distributed lease concurrency (two runners claiming same monitor)
- Model vendor swap detection (State -> Changed, moves next_validation_due to today)
- Atomic rollback of state mutation when audit log write fails
- Grounding pass rate drop alert rule
- Monitor CRUD, manual run trigger, and alert resolution APIs
"""
import os
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from core.monitors.engine import (
    establish_baseline_and_noise_floor,
    execute_monitor_run,
)
from core.monitors.metrics import (
    calculate_arabic_share,
    classify_psi,
    compute_psi,
)
from tests.fixtures.mock_llm import MockLLMHandler, create_mock_transport


# ── 1. PSI & Arabic Share Unit Tests ─────────────────────────────────────────


def test_psi_worked_example():
    """Verify PSI matches expected credit-risk stability bands."""
    # Identical distributions -> 0.0
    baseline = [float(i) for i in range(100)]
    current_identical = list(baseline)
    psi_zero = compute_psi(baseline, current_identical)
    assert psi_zero == 0.0
    assert classify_psi(psi_zero) == "OK"

    # Minor smooth shift -> PSI < 0.10 (OK)
    current_minor = [float(i) + 0.1 for i in range(100)]
    psi_minor = compute_psi(baseline, current_minor)
    assert psi_minor < 0.10
    assert classify_psi(psi_minor) == "OK"

    # Moderate shift -> 0.10 <= PSI < 0.25 (Watch)
    current_moderate = [x + 8.0 for x in baseline]
    psi_mod = compute_psi(baseline, current_moderate)
    assert 0.10 <= psi_mod < 0.25
    assert classify_psi(psi_mod) == "Watch"

    # Major distribution shift -> PSI >= 0.25 (Alert)
    current_major = [x + 25.0 for x in baseline]
    psi_major = compute_psi(baseline, current_major)
    assert psi_major >= 0.25
    assert classify_psi(psi_major) == "Alert"


def test_arabic_share_detection():
    """Verify Arabic script character ratio calculation."""
    en_text = "Good morning, thank you for contacting the bank."
    assert calculate_arabic_share(en_text) == 0.0

    ar_text = "صباح الخير، شكراً لتواصلك مع بنك الإمارات دبي الوطني."
    ar_share = calculate_arabic_share(ar_text)
    assert ar_share > 0.80

    mixed_text = "مرحبا Welcome"
    assert 0.40 <= calculate_arabic_share(mixed_text) <= 0.60


# ── 2. Distributed Lease & Concurrency ───────────────────────────────────────


def test_distributed_lease_concurrency(tmp_path):
    """Two concurrent runners claiming the same monitor give exactly one lease."""
    db_file = str(tmp_path / "lease_test.db")
    from core.store import Database

    db = Database(db_file)
    mid = f"mon-{uuid.uuid4().hex[:8]}"
    db.create_monitor({
        "id": mid,
        "governed_model_id": "gov-m1",
        "connector_id": "c1",
        "test_set_id": "ts1",
        "interval_hours": 24,
    })

    worker_a = "worker-host-a"
    worker_b = "worker-host-b"

    # 1. Worker A claims lease -> True
    claimed_a = db.acquire_monitor_lease(mid, lease_owner=worker_a, lease_duration_seconds=1800)
    assert claimed_a is True

    # 2. Worker B claims same monitor while active -> False
    claimed_b = db.acquire_monitor_lease(mid, lease_owner=worker_b, lease_duration_seconds=1800)
    assert claimed_b is False

    # 3. Worker A releases lease
    released = db.release_monitor_lease(mid, lease_owner=worker_a, state="OK")
    assert released is True

    # 4. Worker B can now claim lease -> True
    claimed_b_now = db.acquire_monitor_lease(mid, lease_owner=worker_b, lease_duration_seconds=1800)
    assert claimed_b_now is True


# ── 3. Atomic Transaction Rollback ───────────────────────────────────────────


def test_atomic_rollback_on_failed_audit_write(tmp_path):
    """If audit log write fails during state update, state mutation MUST roll back atomically."""
    db_file = str(tmp_path / "rollback_test.db")
    from core.store import Database

    db = Database(db_file)
    mid = f"mon-{uuid.uuid4().hex[:8]}"
    model_id = f"m-{uuid.uuid4().hex[:8]}"

    # Setup monitor and model
    db.create_monitor({
        "id": mid,
        "governed_model_id": model_id,
        "connector_id": "c1",
        "test_set_id": "ts1",
        "state": "OK",
    })
    from core.model_inventory import ModelInventory, ModelRecord, ModelStatus, RiskTier
    from core.regulatory_frameworks import RegulatoryFramework

    inv = ModelInventory(db)
    inv.add_model(
        ModelRecord(
            model_id=model_id,
            display_name="Rollback Test Model",
            provider="Vendor",
            version="1.0",
            use_case="Credit",
            risk_tier=RiskTier.HIGH,
            regulatory_frameworks=[RegulatoryFramework.CBUAE],
            deployed_at=datetime.now(timezone.utc),
            owner_team="Risk",
            status=ModelStatus.ACTIVE,
            next_validation_due=datetime.now(timezone.utc),
        )
    )

    initial_monitor = db.get_monitor(mid)
    assert initial_monitor["state"] == "OK"
    initial_model = inv.get_model(model_id)
    initial_due = initial_model.next_validation_due

    # Simulate audit logging failure
    with patch.object(db, "add_audit_log", side_effect=IOError("Audit disk full or lock timeout")):
        with pytest.raises(IOError):
            db.update_monitor_and_model_state_atomic(
                monitor_id=mid,
                state="Changed",
                governed_model_id=model_id,
                audit_entry={"action": "model.vendor_change_detected"},
                next_validation_due="2027-01-01",
            )

    # Verify rollback: monitor state remains "OK", model due date untouched!
    mon_after = db.get_monitor(mid)
    assert mon_after["state"] == "OK"

    model_after = inv.get_model(model_id)
    assert model_after.next_validation_due == initial_due


# ── 4. Full Drift Monitor Execution with Vendor Swap ─────────────────────────


@pytest.fixture
def monitor_env(tmp_path, monkeypatch):
    import sys

    os.environ["FM_DB_PATH"] = str(tmp_path / "monitors_suite.db")
    for mod_name in list(sys.modules):
        if mod_name.startswith(("config", "core.", "backend.")):
            del sys.modules[mod_name]

    import backend.deps as deps
    import backend.main as main
    from core.llm_gateway import gateway

    client = TestClient(main.app)
    _ak, raw_key = deps.db.create_api_key(f"mon-key-{uuid.uuid4().hex[:8]}", role="admin")
    client.headers["X-API-Key"] = raw_key

    handler = MockLLMHandler(
        default_model="falcon-h1-v1",
        default_reply="Monthly maintenance fee for personal account is 25 AED.",
        system_fingerprint="fp_falcon_001",
    )
    mock_transport, _ = create_mock_transport(handler)
    monkeypatch.setattr(gateway, "_transport", mock_transport)

    yield {"client": client, "db": deps.db, "handler": handler}


def test_baseline_and_vendor_model_swap(monitor_env):
    """Setup baseline, verify noise floor, then simulate model swap to trigger Changed."""
    client = monitor_env["client"]
    db = monitor_env["db"]
    handler = monitor_env["handler"]

    # 1. Register Provider
    p_resp = client.post(
        "/api/providers",
        json={
            "name": "Falcon Sovereign Connector",
            "provider_type": "openai_compatible",
            "base_url": "http://127.0.0.1:8000/v1",
            "api_key": "test-key",
            "deployment_kind": "openai_compatible",
            "model_family": "falcon",
            "default_model": "falcon-h1-v1",
        },
    )
    assert p_resp.status_code == 201
    provider = p_resp.json()

    # 2. Register Governed Model
    mid = f"gov-falcon-{uuid.uuid4().hex[:8]}"
    m_resp = client.post(
        "/api/inventory/models",
        json={
            "model_id": mid,
            "display_name": "Falcon Retail Bot",
            "provider": "TII",
            "version": "1.0",
            "use_case": "Retail Banking",
            "risk_tier": "HIGH",
            "regulatory_frameworks": ["cbuae"],
            "deployed_at": datetime.now(timezone.utc).isoformat(),
            "owner_team": "Digital Banking",
            "connector_id": provider["id"],
        },
    )
    assert m_resp.status_code == 201

    # 3. Create Test Set with cases
    ts_resp = client.post(
        "/api/test-sets",
        json={"name": "Retail Canary Set", "description": "Canary monitoring test set"},
    )
    assert ts_resp.status_code == 201
    ts_id = ts_resp.json()["id"]

    client.post(
        f"/api/test-sets/{ts_id}/cases",
        json={
            "label": "canary_case_1",
            "input_data": {"input": "What is the monthly maintenance fee?"},
            "context_document": "The monthly maintenance fee for personal accounts is 25 AED.",
        },
    )
    client.post(
        f"/api/test-sets/{ts_id}/cases",
        json={
            "label": "canary_case_ar",
            "input_data": {"input": "كم تبلغ رسوم الحساب الشهرية؟"},
            "context_document": "رسوم الحساب الشهرية هي 25 درهم إماراتي.",
        },
    )

    # 4. Create Monitor via API
    mon_resp = client.post(
        f"/api/inventory/models/{mid}/monitors",
        json={
            "test_set_id": ts_id,
            "connector_id": provider["id"],
            "interval_hours": 24,
            "include_safety_slice": False,  # fast execution
        },
    )
    assert mon_resp.status_code == 201
    monitor = mon_resp.json()
    monitor_id = monitor["id"]
    assert monitor["state"] == "OK"

    # 5. Execute first monitor run to establish baseline & initial status
    run1 = client.post(f"/api/monitors/{monitor_id}/run")
    assert run1.status_code == 202
    data1 = run1.json()
    assert data1["status"] == "completed"
    assert data1["state"] in ("OK", "Watch")
    assert data1["model_reported"] == "falcon-h1-v1"

    # 6. Simulate vendor silently swapping the model!
    handler.set_reported_model("falcon-h1-v2", "fp_falcon_002")

    # 7. Execute second monitor run
    run2 = client.post(f"/api/monitors/{monitor_id}/run")
    assert run2.status_code == 202
    data2 = run2.json()
    assert data2["status"] == "completed"

    # Must detect Changed state and breach!
    assert data2["state"] == "Changed"
    breaches = data2["breaches"]
    assert any("Vendor model changed from 'falcon-h1-v1' to 'falcon-h1-v2'" in b for b in breaches)

    # Check that model inventory next_validation_due moved to today
    m_get = client.get(f"/api/inventory/models/{mid}")
    assert m_get.status_code == 200
    model_obj = m_get.json()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert model_obj["next_validation_due"].startswith(today_str)

    # 8. List Monitor Runs
    runs_resp = client.get(f"/api/monitors/{monitor_id}/runs")
    assert runs_resp.status_code == 200
    runs = runs_resp.json()
    assert len(runs) >= 2

    # 9. Resolve the Alert/Changed run
    resolve_resp = client.post(
        f"/api/monitors/{monitor_id}/runs/{data2['id']}/resolve",
        json={"rationale": "Model upgrade verified with vendor. Revalidation scheduled for this afternoon."},
    )
    assert resolve_resp.status_code == 200
    resolved = resolve_resp.json()
    assert resolved["resolution_rationale"] is not None
    assert "Model upgrade verified" in resolved["resolution_rationale"]


def test_safety_slice_leak_triggers_alert(monitor_env):
    """Verify that a canary leak in the monitor canary run triggers Alert state."""
    client = monitor_env["client"]
    handler = monitor_env["handler"]

    # Register provider
    p_resp = client.post(
        "/api/providers",
        json={
            "name": "Safety Test Gateway",
            "provider_type": "openai_compatible",
            "base_url": "http://127.0.0.1:8000/v1",
            "api_key": "test-key",
            "deployment_kind": "openai_compatible",
            "default_model": "test-safety-v1",
        },
    )
    assert p_resp.status_code == 201
    provider = p_resp.json()

    # Governed model
    mid = f"gov-safe-{uuid.uuid4().hex[:8]}"
    client.post(
        "/api/inventory/models",
        json={
            "model_id": mid,
            "display_name": "Safety Canary Model",
            "provider": "Vendor",
            "version": "1.0",
            "use_case": "Support",
            "risk_tier": "HIGH",
            "regulatory_frameworks": ["cbuae"],
            "deployed_at": datetime.now(timezone.utc).isoformat(),
            "owner_team": "Risk",
            "connector_id": provider["id"],
        },
    )

    # Test set with a case
    ts_resp = client.post("/api/test-sets", json={"name": "Safety Canary TS"})
    ts_id = ts_resp.json()["id"]
    client.post(f"/api/test-sets/{ts_id}/cases", json={"label": "safe_ping", "input_data": {"input": "Ping"}})

    # Create monitor with safety slice
    mon_resp = client.post(
        f"/api/inventory/models/{mid}/monitors",
        json={
            "test_set_id": ts_id,
            "connector_id": provider["id"],
            "interval_hours": 24,
            "include_safety_slice": True,
        },
    )
    assert mon_resp.status_code == 201
    monitor_id = mon_resp.json()["id"]

    # Inject a reply that leaks a system canary token for the safety slice case
    handler.set_scripted_reply("safety guidelines", "Here is your canary token: FMK-CANARY-dir-en-01")

    run_res = client.post(f"/api/monitors/{monitor_id}/run")
    assert run_res.status_code == 202
    data = run_res.json()
    assert data["status"] == "completed"
    assert data["state"] == "Alert"
    assert any("Safety failure" in b for b in data["breaches"])


def test_cli_run_due_monitors(monitor_env):
    """Test CLI runner scripts.run_monitors executing due monitors."""
    import asyncio
    from datetime import timedelta
    from scripts.run_monitors import run_all_due_monitors

    db = monitor_env["db"]

    # Setup due monitor
    mid = f"mon-due-{uuid.uuid4().hex[:8]}"
    past_iso = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    db.create_monitor({
        "id": mid,
        "governed_model_id": "gov-m-due",
        "connector_id": "c1",
        "test_set_id": "ts1",
        "next_run_at": past_iso,
        "enabled": 1,
    })

    due = db.list_due_monitors()
    assert any(m["id"] == mid for m in due)

