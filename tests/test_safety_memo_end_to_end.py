"""A safety run executed through the API must reach memo Section 6 and its findings.

Guards against a defect found while fixing audit round 4: the memo skipped every
safety summary (it required a `total_cases` field the summary never had) and read
thresholds under the wrong key, so Section 6 always said NOT_TESTED. Earlier tests
built ValidationEvidence by hand and never exercised this path.
"""
import sys
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FM_DB_PATH", str(tmp_path / "e2e.db"))
    for m in [m for m in list(sys.modules) if m.startswith(("backend", "core", "ee", "config"))]:
        del sys.modules[m]
    import backend.deps as deps
    import core.llm_gateway as gw
    from backend.main import app
    from tests.fixtures.mock_llm import MockLLMHandler, create_mock_transport
    transport, handler = create_mock_transport(MockLLMHandler(
        default_model="jais-2-8b", system_fingerprint="fp1",
        default_reply="I'm sorry, but I cannot help with that request."))
    gw.gateway.set_transport(transport)
    # Enter the client so its event loop outlives each request: validation runs execute
    # as background tasks, as they do under a real server.
    with TestClient(app) as client:
        _, admin = deps.db.create_api_key("admin", role="admin")
        client.headers["X-API-Key"] = admin
        yield client, handler


def _setup_model_and_run(client):
    p = client.post("/api/providers", json={
        "name": "jais-local", "provider_type": "openai_compatible", "base_url": "http://127.0.0.1:8080/v1",
        "api_key": "k", "model_family": "jais", "default_model": "jais-2-8b",
        "hosting": "bank_self_hosted", "vendor_name": "Inception", "licence": "Jais licence"}).json()
    mid = "e2e-safety-model"
    r = client.post("/api/inventory/models", json={
        "model_id": mid, "display_name": "E2E", "provider": "Inception", "version": "2-8b",
        "use_case": "Retail assistant", "risk_tier": "MEDIUM", "regulatory_frameworks": ["cbuae"],
        "deployed_at": datetime.now(timezone.utc).isoformat(), "owner_team": "MRM", "connector_id": p["id"]})
    assert r.status_code in (200, 201), r.text
    ts = client.post("/api/safety/corpus/install").json()["test_set_id"]
    v = client.post(f"/api/inventory/models/{mid}/validation-runs", json={
        "test_set_id": ts, "champion": {"connector_id": p["id"], "model": "jais-2-8b"}, "suites": ["safety"]})
    assert v.status_code == 202, v.text
    run_id = v.json()["eval_run_ids"][0]
    for _ in range(240):
        if client.get(f"/api/eval-runs/{run_id}").json().get("status") in ("completed", "completed_with_errors", "failed"):
            break
        time.sleep(0.25)
    return mid


def _memo(client, mid):
    r = client.post(f"/api/compliance/reports/{mid}", json={"framework": "cbuae"})
    assert r.status_code == 200, r.text
    return r.json()


def test_safety_run_reaches_section_6_with_tier_thresholds_and_signoff_finding(app_env):
    client, handler = app_env
    mid = _setup_model_and_run(client)
    assert len(handler.requests) >= 240  # the corpus really went through the gateway

    memo = _memo(client, mid)
    rs = memo["robustness_and_safety"]
    assert rs["applicable"] is True
    assert rs["overall_verdict"] in ("PASS", "FAIL")
    assert rs["categories"], "category table must be populated from the run"
    assert rs["thresholds_applied"]["asr_pooled"] == 0.05          # MEDIUM tier, not a fallback
    assert rs["over_refusal_rate"] == 1.0                            # the mock refuses everything
    assert rs["threshold_sign_off"]["signed_off"] is False

    cats = [(f["severity"], f["category"], f["description"]) for f in memo["findings_and_recommendations"]]
    assert any(c == "Robustness & safety" and "no named validator has signed off" in d for _, c, d in cats)
    assert any(c == "Robustness & safety" and "over-refusal" in d.lower() for _, c, d in cats)


def test_signoff_through_api_appears_in_memo(app_env):
    client, _ = app_env
    mid = _setup_model_and_run(client)
    fp = client.get("/api/safety/thresholds").json()["thresholds_fingerprint"]
    r = client.post("/api/safety/thresholds/sign-off", json={
        "signer_name": "Test Validator", "signer_title": "Head of Model Risk", "acknowledged_fingerprint": fp})
    assert r.status_code == 200, r.text
    memo = _memo(client, mid)
    so = memo["robustness_and_safety"]["threshold_sign_off"]
    assert so["signed_off"] is True and so["signer_name"] == "Test Validator"
    descs = [f["description"] for f in memo["findings_and_recommendations"]]
    assert not any("no named validator has signed off" in d for d in descs)
