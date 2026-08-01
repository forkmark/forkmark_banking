"""Integration tests for the banking API routes (regulatory, inventory,
statistics, compliance). Uses the app TestClient with unique model IDs so the
tests are independent of any residual inventory data."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

import backend.main as main_module

client = TestClient(main_module.app)


@pytest.fixture()
def fresh_app(tmp_path):
    """A freshly-built app and the *same-generation* ``db`` it writes to.

    Tests that seed evidence through ``db`` directly and then read it back
    through the API need both to be the one same Store. Other suites reload
    ``backend.*`` in place, so the module-level ``client`` and a separately
    imported ``db`` can end up on different generations — surfacing as a
    ``FOREIGN KEY constraint failed`` only in a full single-process run. Doing
    one ``del sys.modules`` + re-import here yields a client and a db that are
    guaranteed consistent, and a private DB file so the test is order-independent.
    """
    import sys
    os.environ["FM_DB_PATH"] = str(tmp_path / "banking.db")
    for mod_name in list(sys.modules):
        if mod_name.startswith(("config", "core.", "backend.")):
            del sys.modules[mod_name]
    import backend.deps as deps
    import backend.main as main
    fresh_client = TestClient(main.app)
    try:
        yield fresh_client, deps.db
    finally:
        if hasattr(deps.db, "close"):
            deps.db.close()


def _mid() -> str:
    return f"m-{uuid.uuid4().hex[:10]}"


def _payload(model_id: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model_id": model_id,
        "display_name": f"Model {model_id}",
        "provider": "anthropic",
        "version": "1.0",
        "use_case": "credit adjudication",
        "risk_tier": "HIGH",
        "regulatory_frameworks": ["eu_ai_act"],
        "deployed_at": "2025-01-01T00:00:00+00:00",
        "owner_team": "Model Risk",
        "present_artifacts": [],
    }
    body.update(overrides)
    return body


# ── Regulatory ───────────────────────────────────────────────────────────────


def test_list_and_get_frameworks() -> None:
    r = client.get("/api/regulatory/frameworks")
    assert r.status_code == 200
    frameworks = {f["framework"] for f in r.json()}
    assert frameworks == {"cbuae_mms", "cbuae", "uae_enabling_tech", "eu_ai_act", "sr_26_2", "pra_ss1_23"}

    one = client.get("/api/regulatory/frameworks/eu_ai_act")
    assert one.status_code == 200
    assert one.json()["bias_test_required"] is True

    assert client.get("/api/regulatory/frameworks/not_real").status_code == 404


def test_model_coverage_endpoint() -> None:
    mid = _mid()
    client.post("/api/inventory/models", json=_payload(
        mid, present_artifacts=["validation_memo", "technical_documentation"]))
    r = client.get(f"/api/regulatory/models/{mid}/coverage")
    assert r.status_code == 200
    body = r.json()
    assert body["model_id"] == mid
    assert body["overall_complete"] is False
    fw = body["frameworks"][0]
    assert "conformity_assessment" in fw["missing"]

    assert client.get("/api/regulatory/models/nonexistent/coverage").status_code == 404


# ── Inventory ────────────────────────────────────────────────────────────────


def test_inventory_crud_flow() -> None:
    mid = _mid()
    created = client.post("/api/inventory/models", json=_payload(mid))
    assert created.status_code == 201
    assert created.json()["risk_tier"] == "HIGH"

    # Duplicate -> 409
    assert client.post("/api/inventory/models", json=_payload(mid)).status_code == 409

    got = client.get(f"/api/inventory/models/{mid}")
    assert got.status_code == 200 and got.json()["model_id"] == mid

    listed = client.get("/api/inventory/models")
    assert any(m["model_id"] == mid for m in listed.json())

    patched = client.patch(
        f"/api/inventory/models/{mid}", json={"status": "UNDER_REVIEW", "risk_tier": "CRITICAL"}
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "UNDER_REVIEW"
    assert patched.json()["risk_tier"] == "CRITICAL"

    assert client.delete(f"/api/inventory/models/{mid}").status_code == 204
    assert client.get(f"/api/inventory/models/{mid}").status_code == 404
    assert client.delete(f"/api/inventory/models/{mid}").status_code == 404


def test_due_for_revalidation_endpoint() -> None:
    mid = _mid()
    long_ago = (datetime.now(timezone.utc) - timedelta(days=350)).isoformat()
    client.post("/api/inventory/models", json=_payload(mid, last_validated_at=long_ago))
    r = client.get("/api/inventory/models/due-for-revalidation?days_ahead=30")
    assert r.status_code == 200
    assert any(m["model_id"] == mid for m in r.json())


# ── Statistics ───────────────────────────────────────────────────────────────


def test_statistics_analyze_single_and_batch() -> None:
    single = client.post("/api/statistics/analyze", json={
        "scores_a": [0.9, 0.88, 0.91, 0.87, 0.92],
        "scores_b": [0.2, 0.25, 0.22, 0.24, 0.21],
    })
    assert single.status_code == 200
    body = single.json()
    assert body["multiple_comparison_correction"] is False
    assert body["results"][0]["is_significant"] is True
    assert "Win rate" in body["results"][0]["plain_english"]

    batch = client.post("/api/statistics/analyze", json={"comparisons": [
        {"scores_a": [0.9, 0.9, 0.9, 0.9], "scores_b": [0.1, 0.1, 0.1, 0.1]},
        {"scores_a": [0.5, 0.5, 0.5, 0.5], "scores_b": [0.5, 0.5, 0.5, 0.5]},
    ]})
    assert batch.status_code == 200
    assert batch.json()["multiple_comparison_correction"] is True
    assert len(batch.json()["results"]) == 2

    # Neither single nor batch -> 422 (schema validation).
    assert client.post("/api/statistics/analyze", json={"alpha": 0.05}).status_code == 422


def test_statistics_power_analysis() -> None:
    r = client.post("/api/statistics/power-analysis", json={"effect_size": 0.5})
    assert r.status_code == 200
    assert 60 <= r.json()["minimum_sample_size_per_branch"] <= 68
    # effect_size 0 is rejected by the analyzer.
    assert client.post("/api/statistics/power-analysis", json={"effect_size": 0.0}).status_code == 400


# ── Compliance ───────────────────────────────────────────────────────────────


def _report_body() -> dict[str, Any]:
    return {
        "framework": "eu_ai_act",
        "statistical_comparisons": [
            {"scores_a": [0.9, 0.88, 0.91, 0.87], "scores_b": [0.2, 0.25, 0.22, 0.24]}
        ],
        "bias_groups": {"group_a": 0.9, "group_b": 0.5},
        "numerical_checks": [
            {"source_document": "Net income was $4.2 million.",
             "model_output": "Net income was $9.9 million."}
        ],
        "evaluator_suite": ["numerical_fidelity", "bias_disparity"],
    }


def test_compliance_report_json_and_history() -> None:
    mid = _mid()
    client.post("/api/inventory/models", json=_payload(mid))

    r = client.post(f"/api/compliance/reports/{mid}", json=_report_body())
    assert r.status_code == 200
    memo = r.json()
    assert memo["executive_summary"]["model_id"] == mid
    assert memo["regulatory_mapping"]["missing_count"] > 0
    categories = {f["category"] for f in memo["findings_and_recommendations"]}
    assert "Bias & fairness" in categories

    history = client.get(f"/api/compliance/reports/{mid}/history")
    assert history.status_code == 200
    assert len(history.json()) >= 1
    assert history.json()[0]["framework"] == "eu_ai_act"

    # Unknown model -> 404.
    assert client.post("/api/compliance/reports/ghost", json=_report_body()).status_code == 404


def test_compliance_report_docx_download() -> None:
    mid = _mid()
    client.post("/api/inventory/models", json=_payload(mid))
    r = client.post(f"/api/compliance/reports/{mid}/docx", json=_report_body())
    assert r.status_code == 200
    assert "wordprocessingml.document" in r.headers["content-type"]
    assert len(r.content) > 1000  # a real .docx has non-trivial size


# ── Governed-model linkage → auto-assembled memo ─────────────────────────────
#
# Regression guard. The memo's auto-assembly path reads a model's linked
# validation runs (eval_runs.governed_model_id). That column was previously
# only ever set by the demo seeder, so clicking "Generate Validation Report"
# on a customer's own model returned a memo with no evidence in it — a silent
# 200 with empty sections. These tests pin the link open.


def _link_fixture(fresh) -> tuple[str, str, str]:
    """Register a model, ingest a champion/challenger pair against it, and
    return (model_id, eval_run_id, comparison_id). ``fresh`` is the
    ``fresh_app`` (client, db) pair so writes and reads share one Store."""
    api, db = fresh
    mid = _mid()
    api.post("/api/inventory/models", json=_payload(
        mid, regulatory_frameworks=["cbuae"]))
    er = api.post("/api/eval-runs", json={
        "workflow_name": f"wf-{mid}", "name": "champion vs challenger",
        "total_cases": 1, "governed_model_id": mid})
    assert er.status_code == 201
    er_id = er.json()["id"]

    wf_id = er.json()["workflow_id"]
    run = db.create_run(workflow_id=wf_id, input_data={}, eval_run_id=er_id)
    a = db.create_branch(run.id, wf_id, "champion", "v1", is_baseline=True)
    b = db.create_branch(run.id, wf_id, "challenger", "v2")
    db.save_step_output(run.id, a.id, "reply", 0, [],
                        "The fee is AED 241.50 on a balance of AED 12,450.00.", "v1")
    db.save_step_output(run.id, b.id, "reply", 0, [],
                        "We can offer a guaranteed profit rate of 3.75%.", "v2")
    comp = db.create_comparison(run.id, wf_id, a.id, b.id, eval_run_id=er_id)
    return mid, er_id, comp.id


def test_eval_run_carries_governed_model_id(fresh_app) -> None:
    api, _ = fresh_app
    mid, er_id, _ = _link_fixture(fresh_app)
    assert api.get(f"/api/eval-runs/{er_id}").json()["governed_model_id"] == mid


def test_link_route_attaches_run_to_model(fresh_app) -> None:
    api, _ = fresh_app
    mid = _mid()
    api.post("/api/inventory/models", json=_payload(mid))
    er_id = api.post("/api/eval-runs", json={
        "workflow_name": f"wf-{mid}", "name": "unlinked"}).json()["id"]
    assert api.get(f"/api/eval-runs/{er_id}").json()["governed_model_id"] is None

    r = api.post(f"/api/eval-runs/{er_id}/governed-model", json={"model_id": mid})
    assert r.status_code == 200
    assert api.get(f"/api/eval-runs/{er_id}").json()["governed_model_id"] == mid

    # Both sides of the link must exist.
    assert api.post(f"/api/eval-runs/{er_id}/governed-model",
                    json={"model_id": "no-such-model"}).status_code == 404
    assert api.post("/api/eval-runs/ghost/governed-model",
                    json={"model_id": mid}).status_code == 404


def test_one_click_memo_computes_evidence_from_linked_run(fresh_app) -> None:
    """The UI sends only {framework}. That must still yield computed evidence."""
    api, _ = fresh_app
    mid, _, comp_id = _link_fixture(fresh_app)
    api.post(f"/api/comparisons/{comp_id}/decide", json={
        "reviewer_id": "validator", "choice": "A", "confidence": "high",
        "rationale_for_choice": "Challenger asserts an unsupported profit rate."})

    memo = api.post(f"/api/compliance/reports/{mid}", json={"framework": "cbuae"})
    assert memo.status_code == 200
    body = memo.json()

    nf = body["numerical_fidelity"]
    assert nf["applicable"] is True, "auto-assembly produced an empty memo"
    flagged = [f for a in nf["assessments"] for f in a["flagged_numbers"]]
    assert any(f["value"] == 3.75 for f in flagged), \
        "the hallucinated profit rate was not flagged"

    assert body["human_review_summary"]["total_decisions"] == 1
    assert "NumericalFidelityEvaluator" in body["scope_and_methodology"]["evaluator_suite"]
