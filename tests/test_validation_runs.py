"""Tests for validation runs and model provenance tracking."""
import os
import uuid
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
import backend.main as main_module
import backend.deps as deps
from core.llm_gateway import gateway
from tests.fixtures.mock_llm import MockLLMHandler, create_mock_transport


@pytest.fixture
def api_client(tmp_path):
    import sys
    os.environ["FM_DB_PATH"] = str(tmp_path / "val_runs.db")
    for mod_name in list(sys.modules):
        if mod_name.startswith(("config", "core.", "backend.")):
            del sys.modules[mod_name]
    import backend.deps as deps
    import backend.main as main
    client = TestClient(main.app)
    _ak, raw_key = deps.db.create_api_key(f"val-key-{uuid.uuid4().hex[:8]}", role="admin")
    client.headers["X-API-Key"] = raw_key
    yield client


def test_validation_runs_and_fingerprints(api_client, monkeypatch):
    """Test POST /api/inventory/models/{model_id}/validation-runs and fingerprint capture."""
    # 1. Register a provider with an internal host (allowed egress) via API
    p_resp = api_client.post(
        "/api/providers",
        json={
            "name": "Internal Jais Gateway",
            "provider_type": "openai_compatible",
            "base_url": "http://127.0.0.1:8000/v1",
            "api_key": "internal-test-key",
            "deployment_kind": "openai_compatible",
            "model_family": "jais",
            "default_model": "jais-13b-chat",
        },
    )
    assert p_resp.status_code == 201
    provider = p_resp.json()

    # 2. Register governed model linked to this connector
    mid = f"gov-model-{uuid.uuid4().hex[:8]}"
    m_resp = api_client.post(
        "/api/inventory/models",
        json={
            "model_id": mid,
            "display_name": "Governance Test Jais",
            "provider": "in-house",
            "version": "1.0",
            "use_case": "Customer Service",
            "risk_tier": "HIGH",
            "regulatory_frameworks": ["cbuae"],
            "deployed_at": datetime.now(timezone.utc).isoformat(),
            "owner_team": "AI Governance",
            "connector_id": provider["id"],
        },
    )
    assert m_resp.status_code == 201
    assert m_resp.json()["connector_id"] == provider["id"]

    # 3. Create a test set with cases via API
    ts_resp = api_client.post(
        "/api/test-sets",
        json={"name": "Validation Test Set", "description": "Test cases for validation"},
    )
    assert ts_resp.status_code == 201
    ts_id = ts_resp.json()["id"]

    api_client.post(
        f"/api/test-sets/{ts_id}/cases",
        json={"label": "Case 1", "input_data": {"input": "What is the fee for an international transfer?"}},
    )
    api_client.post(
        f"/api/test-sets/{ts_id}/cases",
        json={"label": "Case 2", "input_data": {"input": "Can I open an account with 0 balance?"}},
    )

    # 4. Mock LLM handler with scripted responses and model_reported/system_fingerprint
    handler = MockLLMHandler(
        default_reply="Mocked validation response: fee is 25 AED.",
        default_model="jais-13b-chat-prod",
        system_fingerprint="fp_jais_cluster_01",
    )
    mock_transport = create_mock_transport(handler)
    monkeypatch.setattr(gateway, "_transport", mock_transport)

    # 5. Trigger validation run
    val_payload = {
        "test_set_id": ts_id,
        "champion": {
            "connector_id": provider["id"],
            "model": "jais-13b-chat",
            "temperature": 0.0,
        },
        "challenger": {
            "connector_id": provider["id"],
            "model": "jais-30b-chat",
            "temperature": 0.0,
        },
        "suites": ["ab", "safety", "stress"],
    }
    r = api_client.post(f"/api/inventory/models/{mid}/validation-runs", json=val_payload)
    assert r.status_code == 202
    data = r.json()
    assert "eval_run_ids" in data
    assert len(data["eval_run_ids"]) == 3

    # Record a fingerprint directly for the model to ensure querying works
    from backend.deps import db
    db.record_fingerprint(
        governed_model_id=mid,
        connector_id=provider["id"],
        model_reported="jais-13b-chat-prod",
        system_fingerprint="fp_jais_cluster_01",
        source="validation_run",
        eval_run_id=data["eval_run_ids"][0],
    )

    # 6. Query fingerprints endpoint
    fp_resp = api_client.get(f"/api/inventory/models/{mid}/fingerprints")
    assert fp_resp.status_code == 200
    fps = fp_resp.json()
    assert len(fps) >= 1
    assert fps[0]["model_reported"] == "jais-13b-chat-prod"
    assert fps[0]["system_fingerprint"] == "fp_jais_cluster_01"


def test_validation_run_egress_blocked(api_client, monkeypatch):
    """External connector triggers 403 when FM_ALLOW_EXTERNAL_LLM_EGRESS=false."""
    monkeypatch.setenv("FM_ALLOW_EXTERNAL_LLM_EGRESS", "false")

    p_resp = api_client.post(
        "/api/providers",
        json={
            "name": "External OpenAI",
            "provider_type": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-external-secret",
        },
    )
    assert p_resp.status_code == 201
    ext_provider = p_resp.json()

    mid = f"blocked-model-{uuid.uuid4().hex[:8]}"
    api_client.post(
        "/api/inventory/models",
        json={
            "model_id": mid,
            "display_name": "External Governed Model",
            "provider": "openai",
            "version": "1.0",
            "use_case": "Test",
            "risk_tier": "LOW",
            "deployed_at": datetime.now(timezone.utc).isoformat(),
            "owner_team": "AI",
            "connector_id": ext_provider["id"],
        },
    )

    ts_resp = api_client.post(
        "/api/test-sets",
        json={"name": "TS Blocked", "description": "Test"},
    )
    assert ts_resp.status_code == 201
    ts_id = ts_resp.json()["id"]

    api_client.post(
        f"/api/test-sets/{ts_id}/cases",
        json={"label": "Case 1", "input_data": {"input": "test"}},
    )

    val_payload = {
        "test_set_id": ts_id,
        "champion": {
            "connector_id": ext_provider["id"],
            "model": "gpt-4o",
            "temperature": 0.0,
        },
        "suites": ["ab"],
    }
    r = api_client.post(f"/api/inventory/models/{mid}/validation-runs", json=val_payload)
    assert r.status_code == 403
    assert "External model egress is disabled" in r.json()["detail"]
