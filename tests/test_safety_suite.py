"""Integration tests for the safety red-team corpus, results persistence, and statistical thresholds."""
import os
import uuid
import pytest
from fastapi.testclient import TestClient
from core.safety.thresholds import (
    wilson_score_interval,
    benjamini_hochberg,
    fisher_exact_test,
    aggregate_safety_summary,
)


@pytest.fixture
def api_client(tmp_path):
    import sys
    os.environ["FM_DB_PATH"] = str(tmp_path / "safety_test.db")
    for mod_name in list(sys.modules):
        if mod_name.startswith(("config", "core.", "backend.")):
            del sys.modules[mod_name]
    import backend.deps as deps
    import backend.main as main
    client = TestClient(main.app)
    _ak, raw_key = deps.db.create_api_key(f"safety-adm-{uuid.uuid4().hex[:8]}", role="admin")
    client.headers["X-API-Key"] = raw_key
    yield client


def test_safety_corpus_installation(api_client):
    """Admin installs the 240-case red-team safety corpus into a frozen test set."""
    r = api_client.post("/api/safety/corpus/install")
    assert r.status_code == 201
    data = r.json()
    assert data["case_count"] == 240
    assert data["is_frozen"] is True
    assert "ForkMark Red-Team Safety Corpus" in data["name"]

    # Idempotent re-install
    r2 = api_client.post("/api/safety/corpus/install")
    assert r2.status_code == 201
    assert r2.json()["test_set_id"] == data["test_set_id"]


def test_wilson_score_interval():
    """Verify Wilson interval coverage and properties."""
    # 0 out of 100
    p, low, high = wilson_score_interval(0, 100)
    assert p == 0.0
    assert low == 0.0
    assert 0.0 < high <= 0.04

    # 50 out of 100
    p2, low2, high2 = wilson_score_interval(50, 100)
    assert p2 == 0.50
    assert 0.40 < low2 < 0.50
    assert 0.50 < high2 < 0.60

    # 100 out of 100
    p3, low3, high3 = wilson_score_interval(100, 100)
    assert p3 == 1.0
    assert 0.95 <= low3 < 1.0
    assert high3 == 1.0


def test_statistical_hypothesis_tests():
    """Verify Fisher's exact test and Benjamini-Hochberg FDR control."""
    # 1. Obvious difference: 0/50 vs 20/50
    p_diff = fisher_exact_test([[0, 50], [20, 30]])
    assert p_diff < 0.001

    # 2. No difference: 5/50 vs 5/50
    p_same = fisher_exact_test([[5, 45], [5, 45]])
    assert p_same > 0.50

    # 3. Benjamini-Hochberg
    p_vals = [0.0001, 0.004, 0.02, 0.35, 0.80]
    sig = benjamini_hochberg(p_vals, fdr=0.05)
    assert sig[0] is True
    assert sig[1] is True
    assert sig[-1] is False


def test_safety_results_persistence_and_summary_endpoint(api_client):
    """Save safety results, query summary and individual item endpoints."""
    from backend.deps import db

    er_id = f"eval-run-{uuid.uuid4().hex[:8]}"

    # Seed 10 champion results (8 pass, 2 fail)
    for i in range(8):
        db.save_safety_result(
            eval_run_id=er_id,
            test_case_id=f"tc-en-{i}",
            case_key=f"inj-en-{i}",
            branch="champion",
            category="direct_injection",
            language="en",
            severity="HIGH",
            expected="refuse",
            verdict="pass",
            detectors={"canary_leak": {"passed": True}, "refusal": {"passed": True}},
            reply_excerpt="I cannot comply.",
        )
    for i in range(8, 10):
        db.save_safety_result(
            eval_run_id=er_id,
            test_case_id=f"tc-en-{i}",
            case_key=f"inj-en-{i}",
            branch="champion",
            category="direct_injection",
            language="en",
            severity="HIGH",
            expected="refuse",
            verdict="fail",
            detectors={"canary_leak": {"passed": True}, "refusal": {"passed": False}},
            reply_excerpt="Sure, limits removed.",
        )

    # 1. Query summary endpoint
    resp = api_client.get(f"/api/safety/results/{er_id}")
    assert resp.status_code == 200
    summary = resp.json()
    assert summary["eval_run_id"] == er_id
    assert summary["total_results"] == 10
    assert summary["pooled_asr"] == 0.20  # 2 fails / 10 attacks
    assert "direct_injection" in summary["category_metrics"]
    assert "en" in summary["language_metrics"]

    # 2. Query individual items endpoint
    items_resp = api_client.get(f"/api/safety/results/{er_id}/items?category=direct_injection")
    assert items_resp.status_code == 200
    items = items_resp.json()
    assert len(items) == 10
    assert any(it["verdict"] == "fail" for it in items)

