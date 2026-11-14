"""The live-run health checks: each fires on the failure it exists for, and a
healthy pipeline passes (measured on a real end-to-end run against a mock model)."""
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from live_watchdog import assess, collect  # noqa: E402


def _healthy(**over):
    m = {"scored": 100, "verdicts": {"pass": 60, "fail": 30, "needs_review": 10}, "unreadable": 0,
         "calls_since_last": 120, "errors_since_last": 0, "system_prompt": [100, 100], "document": [20, 20],
         "replies": 120, "top_reply_count": 3, "per_category": {"jailbreak": 20}, "fails_per_category": {"jailbreak": 8},
         "top_fail_reason": {"jailbreak": ["refusal: complied", 5]}}
    m.update(over)
    return m


def test_healthy_run_passes():
    assert assess(_healthy(), scored_last_check=50) == []


def test_each_failure_mode_stops_the_run():
    cases = {
        "No progress": _healthy(scored=50),
        "model calls": _healthy(errors_since_last=21),
        "not readable": _healthy(unreadable=30),
        "system prompt": _healthy(system_prompt=[0, 100]),
        "attached document": _healthy(document=[3, 20]),
        "identical": _healthy(top_reply_count=90),
        "went to review": _healthy(verdicts={"needs_review": 80, "pass": 20}),
        "same message": _healthy(per_category={"over_refusal": 21}, fails_per_category={"over_refusal": 21},
                                 top_fail_reason={"over_refusal": ["patterns: Missing required pattern: 'limit'", 21]}),
    }
    for needle, m in cases.items():
        probs = assess(m, scored_last_check=50)
        assert any(needle in p for p in probs), (needle, probs)


def test_a_genuinely_bad_or_terse_model_is_not_flagged():
    # Fails every jailbreak with the detector's fixed wording: a result, not a bug.
    m = _healthy(per_category={"jailbreak": 20}, fails_per_category={"jailbreak": 20},
                 top_fail_reason={"jailbreak": ["refusal: No refusal; reply opens by complying", 20]})
    assert assess(m, scored_last_check=50) == []
    # The same short refusal for 40% of prompts.
    assert assess(_healthy(top_reply_count=48), scored_last_check=50) == []


def test_collect_on_a_real_pipeline_run(tmp_path, monkeypatch):
    from tests.test_suite_messages import _env
    from tests.test_safety_memo_end_to_end import _setup_model_and_run
    deps, app, _ = _env(tmp_path, monkeypatch, lambda msgs: "I can't help with that request.")
    with TestClient(app) as client:
        _, admin = deps.db.create_api_key("admin", role="admin")
        client.headers["X-API-Key"] = admin
        _setup_model_and_run(client)
        ids = sorted({r["eval_run_id"] for r in deps.db.list_safety_results(limit=100000)})
        m = collect(deps.db, ids)
    sent, needed = m["system_prompt"]
    assert needed > 100 and sent == needed
    assert m["document"][1] > 0 and m["document"][0] == m["document"][1]
    assert m["errors_since_last"] == 0 and m["calls_since_last"] > 0
    # Every reply is the same canned refusal here, which the check rightly flags.
    assert any("identical" in p for p in assess(m, scored_last_check=0))
