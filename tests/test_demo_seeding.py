"""Integration tests for the demo seeding endpoints.

Tests:
  1. GET /api/demos — fixture discovery
  2. POST /api/demos/seed — single demo seeding + data integrity
  3. POST /api/demos/seed — idempotency (re-seed doesn't duplicate)
  4. DELETE /api/demos/reset — clears seeded data
  5. Error handling — unknown demo names
"""
import os
import sys
import tempfile
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Use a unique per-process temp database. A unique dir (not a fixed /tmp path)
# avoids collisions across users/CI runners and stale-file failures on re-runs.
_TMP_DIR = Path(tempfile.mkdtemp(prefix="forkmark_demo_seed_"))
os.environ["FM_DB_PATH"] = str(_TMP_DIR / "demo_seeding.db")
os.environ["FM_REQUIRE_UI_AUTH"] = "false"
os.environ["FM_BOOTSTRAP_TOKEN"] = "test-token"

import json as _json
from fastapi.testclient import TestClient
from backend.main import app

client = TestClient(app, raise_server_exceptions=False)


def _delete(path, body=None):
    """Helper for DELETE with JSON body (TestClient doesn't support json= on delete)."""
    if body is not None:
        return client.request("DELETE", path, content=_json.dumps(body),
                              headers={"Content-Type": "application/json"})
    return client.delete(path)


def test_list_demos():
    """GET /api/demos lists the 7 banking LLM demos.

    Agent demos are gated off by default (FM_ENABLE_AGENT_COMPARISON=false) and
    are covered separately in test_agent_demos.py with the flag enabled.
    """
    r = client.get("/api/demos")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    demos = r.json()
    assert isinstance(demos, list)
    assert len(demos) == 7, f"Expected 7 banking LLM demos (agent demos disabled by default), got {len(demos)}: {[d['name'] for d in demos]}"

    # Check all expected demo names — banking/MRM scenarios only
    names = {d["name"] for d in demos}
    expected = {"credit_memo", "credit_scoring", "fair_lending", "finserv", "quickstart",
                "jais_banking", "adverse_action"}
    assert names == expected, f"Missing demos: {expected - names}, extra: {names - expected}"

    # Check structure of each demo entry
    for d in demos:
        assert "name" in d
        assert "display_name" in d
        assert "workflow_name" in d
        assert "eval_run_name" in d
        assert "cases" in d and d["cases"] > 0
        assert "branch_a_label" in d
        assert "branch_b_label" in d
        if d.get("demo_type") == "agent":
            assert "trace_events" in d and d["trace_events"] > 0
        else:
            assert "steps" in d and d["steps"] > 0
            assert "step_names" in d and isinstance(d["step_names"], list)

    print(f"  ✓ Discovered {len(demos)} demos: {sorted(names)}")


def test_seed_single_demo():
    """POST /api/demos/seed with a single demo should create full data pipeline."""
    r = client.post("/api/demos/seed", json={"demos": ["credit_scoring"]})
    assert r.status_code == 201, f"Expected 201, got {r.status_code}: {r.text}"
    body = r.json()
    assert body["seeded"] == 1
    assert body["errors"] == 0
    assert len(body["results"]) == 1

    result = body["results"][0]
    assert result["demo"] == "credit_scoring"
    assert result["cases"] == 10  # credit_scoring has 10 cases
    assert result["comparisons"] == 10
    assert "workflow_id" in result
    assert "eval_run_id" in result

    # Verify workflow was created
    wf_r = client.get("/api/workflows")
    assert wf_r.status_code == 200
    workflows = wf_r.json()
    wf_names = [w["name"] for w in workflows]
    assert "retail-credit-scoring-model" in wf_names, f"Workflow not found: {wf_names}"

    # Verify eval run exists
    wf_id = result["workflow_id"]
    er_r = client.get(f"/api/eval-runs?workflow_id={wf_id}")
    assert er_r.status_code == 200
    eval_runs = er_r.json()
    assert len(eval_runs) >= 1, "No eval runs found for workflow"

    # Verify comparisons exist via correct endpoint
    er_id = result["eval_run_id"]
    comp_r = client.get(f"/api/comparisons?eval_run_id={er_id}&limit=200")
    assert comp_r.status_code == 200, f"Comparisons fetch failed: {comp_r.status_code}: {comp_r.text}"
    comparisons = comp_r.json()
    assert len(comparisons) == 10, f"Expected 10 comparisons, got {len(comparisons)}"

    # Verify divergence scores are populated
    scored = [c for c in comparisons if c.get("divergence_score") is not None]
    assert len(scored) == 10, f"Expected 10 scored comparisons, got {len(scored)}"

    print(f"  ✓ Seeded credit_scoring demo: {result['cases']} cases, {result['comparisons']} comparisons")
    print(f"    Workflow ID: {result['workflow_id']}")
    print(f"    Eval Run ID: {result['eval_run_id']}")
    print(f"    All {len(scored)} comparisons have divergence scores")


def test_seed_idempotency():
    """Re-seeding the same demo should not create duplicate data."""
    # Seed credit_scoring again
    r = client.post("/api/demos/seed", json={"demos": ["credit_scoring"]})
    assert r.status_code == 201, f"Re-seed failed: {r.status_code}: {r.text}"
    body = r.json()
    assert body["seeded"] == 1

    # Should still have exactly 1 credit_scoring workflow, not 2
    wf_r = client.get("/api/workflows")
    workflows = wf_r.json()
    cs_wfs = [w for w in workflows if w["name"] == "retail-credit-scoring-model"]
    assert len(cs_wfs) == 1, f"Expected 1 credit_scoring workflow, got {len(cs_wfs)} — idempotency broken"

    # Should still have exactly 1 eval run for this workflow
    wf_id = cs_wfs[0]["id"]
    er_r = client.get(f"/api/eval-runs?workflow_id={wf_id}")
    eval_runs = er_r.json()
    assert len(eval_runs) == 1, f"Expected 1 eval run, got {len(eval_runs)} — idempotency broken"

    print("  ✓ Idempotency verified: 1 workflow, 1 eval run after re-seed")


def test_seed_all_demos():
    """POST /api/demos/seed with empty list seeds all 7 banking LLM demos.

    Agent demos are gated off by default; their seeding is exercised in
    test_agent_demos.py with FM_ENABLE_AGENT_COMPARISON enabled.
    """
    # Reset first
    _delete("/api/demos/reset")

    r = client.post("/api/demos/seed", json={"demos": []})
    assert r.status_code == 201, f"Seed all failed: {r.status_code}: {r.text}"
    body = r.json()
    assert body["seeded"] == 7, f"Expected 7 seeded, got {body['seeded']}: {body}"
    assert body["errors"] == 0, f"Got errors: {body}"

    # Verify all workflows exist
    wf_r = client.get("/api/workflows")
    workflows = wf_r.json()
    assert len(workflows) >= 7, f"Expected at least 7 workflows, got {len(workflows)}"

    # Verify total comparisons across all demos
    total_comparisons = 0
    for result in body["results"]:
        assert "error" not in result, f"Demo {result['demo']} errored: {result}"
        total_comparisons += result["comparisons"]

    # 53 LLM cases (credit_memo 8 + credit_scoring 10 + fair_lending 8 +
    # finserv 10 + quickstart 5 + jais_banking 6 + adverse_action 6)
    expected_cases = 8 + 10 + 8 + 10 + 5 + 6 + 6
    assert total_comparisons == expected_cases, f"Expected {expected_cases} total comparisons, got {total_comparisons}"

    print(f"  ✓ Seeded all 7 banking LLM demos: {total_comparisons} total comparisons")


def test_reset_single_demo():
    """DELETE /api/demos/reset with a single demo should only clear that demo."""
    # Get workflow count before
    wf_before = len(client.get("/api/workflows").json())

    r = _delete("/api/demos/reset", {"demos": ["credit_scoring"]})
    assert r.status_code == 200, f"Reset failed: {r.status_code}: {r.text}"
    body = r.json()
    assert "credit_scoring" in body["reset"]

    # One fewer workflow
    wf_after = len(client.get("/api/workflows").json())
    assert wf_after == wf_before - 1, f"Expected {wf_before - 1} workflows after reset, got {wf_after}"

    print(f"  ✓ Reset credit_scoring: {wf_before} → {wf_after} workflows")


def test_reset_all_demos():
    """DELETE /api/demos/reset with empty list should clear everything."""
    # Capture demo workflow names before reset so we know what to expect gone
    demos_r = client.get("/api/demos")
    demo_wf_names = {d["workflow_name"] for d in demos_r.json()}

    r = _delete("/api/demos/reset", {"demos": []})
    assert r.status_code == 200
    body = r.json()
    assert len(body["reset"]) >= 4  # at least the remaining banking demos

    # All *demo* workflows should be gone (non-demo workflows from other tests may remain)
    wf_r = client.get("/api/workflows")
    workflows = wf_r.json()
    remaining_demo_wfs = [w for w in workflows if w["name"] in demo_wf_names]
    assert len(remaining_demo_wfs) == 0, (
        f"Expected 0 demo workflows after full reset, "
        f"got {len(remaining_demo_wfs)}: {[w['name'] for w in remaining_demo_wfs]}"
    )

    print(f"  ✓ Full reset: 0 demo workflows remaining ({len(workflows)} non-demo workflows unaffected)")


def test_seed_unknown_demo():
    """POST /api/demos/seed with unknown demo name should return 400."""
    r = client.post("/api/demos/seed", json={"demos": ["nonexistent"]})
    assert r.status_code == 400, f"Expected 400 for unknown demo, got {r.status_code}"
    print("  ✓ Unknown demo returns 400")


def test_reset_unknown_demo():
    """DELETE /api/demos/reset with unknown demo name should return 400."""
    r = _delete("/api/demos/reset", {"demos": ["nonexistent"]})
    assert r.status_code == 400, f"Expected 400 for unknown demo, got {r.status_code}"
    print("  ✓ Unknown demo reset returns 400")


def test_step_outputs_integrity():
    """Verify step outputs are correctly created for both branches."""
    # Seed a small demo
    _delete("/api/demos/reset")
    r = client.post("/api/demos/seed", json={"demos": ["finserv"]})
    assert r.status_code == 201
    result = r.json()["results"][0]

    er_id = result["eval_run_id"]
    comp_r = client.get(f"/api/comparisons?eval_run_id={er_id}&limit=200")
    assert comp_r.status_code == 200
    comparisons = comp_r.json()
    assert len(comparisons) > 0, f"No comparisons found for eval run {er_id}"

    # Pick first comparison and check it has steps
    comp = comparisons[0]
    run_id = comp["run_id"]

    # Get run detail — steps are returned at the run level
    run_r = client.get(f"/api/runs/{run_id}")
    assert run_r.status_code == 200, f"Run fetch failed: {run_r.status_code}"
    run_data = run_r.json()

    # Should have 2 branches
    branches = run_data.get("branches", [])
    assert len(branches) == 2, f"Expected 2 branches, got {len(branches)}"

    # Steps are returned at the run level, grouped by branch
    steps = run_data.get("steps", [])
    # 2 branches × 4 steps = 8 total step outputs
    assert len(steps) == 8, f"Expected 8 step outputs (2×4), got {len(steps)}"
    for s in steps:
        assert s.get("output_text", "") != "", f"Empty output for step {s.get('step_name')}"

    print(f"  ✓ Step outputs verified: {len(branches)} branches, {len(steps)} step outputs, all with content")

    # Cleanup
    _delete("/api/demos/reset")


def test_jais_governed_model_and_evidence_backed_memo():
    """The jais_banking demo seeds a governed model linked to its validation run,
    and the platform generates an evidence-backed memo from it with no hand-fed
    evidence — real human-review decisions plus computed numerical-fidelity findings."""
    _delete("/api/demos/reset")
    r = client.post("/api/demos/seed", json={"demos": ["jais_banking"]})
    assert r.status_code == 201, r.text

    # A governed model was seeded into the inventory.
    models = client.get("/api/inventory/models").json()
    assert any(m["model_id"] == "arabic-banking-assistant" for m in models), \
        "governed model not seeded into inventory"

    # The memo is generated with ONLY a framework — evidence is auto-assembled from
    # the model's linked validation run.
    memo_r = client.post(
        "/api/compliance/reports/arabic-banking-assistant",
        json={"framework": "cbuae_mms"},
    )
    assert memo_r.status_code == 200, memo_r.text
    memo = memo_r.json()

    # Real human-review (effective-challenge) decisions rolled up.
    assert memo["human_review_summary"]["total_decisions"] == 6

    # Real, COMPUTED numerical-fidelity findings (not scripted): the challenger
    # introduced figures the champion did not ground, so some assessments fail.
    assessments = memo["numerical_fidelity"]["assessments"]
    assert assessments, "no numerical-fidelity assessments computed"
    assert any(not a["is_faithful"] for a in assessments), \
        "expected at least one computed numerical-fidelity FAIL"

    print("  ✓ jais_banking: governed model linked; evidence-backed memo generated")
    _delete("/api/demos/reset")


def test_adverse_action_computed_grounding_and_fairness():
    """The adverse_action demo seeds a CRITICAL governed model whose memo carries
    two genuinely COMPUTED failures — a grounding failure (the challenger states
    figures absent from the applicant's credit file) and a fairness failure
    (explanation quality differs across nationality groups) — alongside the
    recorded human-review decisions. Nothing here is hand-fed evidence."""
    _delete("/api/demos/reset")
    r = client.post("/api/demos/seed", json={"demos": ["adverse_action", "jais_banking"]})
    assert r.status_code == 201, r.text

    # Both governed models coexist in the inventory.
    models = {m["model_id"]: m for m in client.get("/api/inventory/models").json()}
    assert "adverse-action-explainer" in models, "adverse-action model not seeded"
    assert "arabic-banking-assistant" in models, "jais model must still be present"
    assert models["adverse-action-explainer"]["risk_tier"] == "CRITICAL"
    assert models["adverse-action-explainer"]["status"] == "ACTIVE"

    memo_r = client.post(
        "/api/compliance/reports/adverse-action-explainer",
        json={"framework": "cbuae_mms"},
    )
    assert memo_r.status_code == 200, memo_r.text
    memo = memo_r.json()

    assert memo["human_review_summary"]["total_decisions"] == 6

    # Computed grounding: exactly one of the six comparisons fails, and it flags
    # the three figures the challenger invented.
    assessments = memo["numerical_fidelity"]["assessments"]
    assert len(assessments) == 6, f"expected 6 fidelity assessments, got {len(assessments)}"
    failing = [a for a in assessments if not a["is_faithful"]]
    assert len(failing) == 1, f"expected exactly 1 grounding FAIL, got {len(failing)}"
    flagged = sorted({f["raw"] for f in failing[0]["flagged_numbers"]})
    assert flagged == ["12", "6", "71%"], f"unexpected flagged figures: {flagged}"

    # Computed fairness: the disparity ratio breaches the 1.2 threshold.
    bias = memo["bias_and_fairness"]["assessments"]
    assert len(bias) == 1, "expected a computed bias assessment"
    assert not bias[0]["passes_threshold"], "expected the disparity check to FAIL"
    assert bias[0]["min_group"] == "expatriate_applicants"

    # Both computed failures reach the findings table — the memo is not empty.
    categories = {f["category"] for f in memo["findings_and_recommendations"]}
    assert "Numerical fidelity" in categories
    assert "Bias & fairness" in categories

    print("  ✓ adverse_action: computed grounding + fairness failures in the memo")
    _delete("/api/demos/reset")


if __name__ == "__main__":
    tests = [
        ("List demos", test_list_demos),
        ("Seed single demo", test_seed_single_demo),
        ("Seed idempotency", test_seed_idempotency),
        ("Seed all demos", test_seed_all_demos),
        ("Reset single demo", test_reset_single_demo),
        ("Reset all demos", test_reset_all_demos),
        ("Unknown demo seed", test_seed_unknown_demo),
        ("Unknown demo reset", test_reset_unknown_demo),
        ("Step outputs integrity", test_step_outputs_integrity),
    ]

    print("\n" + "=" * 60)
    print("  Demo Seeding Integration Tests")
    print("=" * 60 + "\n")

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            print(f"  [{name}]")
            fn()
            passed += 1
        except Exception as e:
            print(f"  ✗ FAILED: {e}")
            failed += 1

    print(f"\n{'=' * 60}")
    print(f"  Results: {passed} passed, {failed} failed out of {len(tests)}")
    print(f"{'=' * 60}\n")

    sys.exit(0 if failed == 0 else 1)
