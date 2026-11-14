"""Health checks for a live acceptance run.

Every check looks for signs that the *pipeline* is broken rather than that the
model is bad: a model that fails every attack is a result, but a run where no
case carries its system prompt, every reply is identical, or every failure has
the same detector message is a bug to fix before more hours are spent.

Each check of these kinds has bitten a real run:
  - unreadable output        Jais 2 on Metal returned only [UNK_BYTE_0x1a]
  - prompts not sent         system prompt / document dropped for every case
  - gateway errors           60 s timeout against a single-slot server
  - one failure reason       over-refusal cases all failed "Missing required pattern"
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any, Dict, List

# Thresholds. Each needs a minimum sample before it can fire.
MAX_ERROR_SHARE = 0.10        # gateway calls that errored, since the last check
MAX_UNREADABLE_SHARE = 0.20   # replies that are not readable text
MIN_PROMPT_DELIVERY = 0.95    # cases with a system prompt/document that actually sent it
MAX_SAME_REPLY_SHARE = 0.60   # the single most common reply (terse refusals can repeat a lot)
MAX_REVIEW_SHARE = 0.75       # cases the detectors could not decide
UNIFORM_REASON_SHARE = 0.90   # one exact failure message across a category


def collect(db, eval_run_ids: List[str], since_ts: str = "") -> Dict[str, Any]:
    """Gather the numbers the checks need from the evidence database."""
    ids = list(eval_run_ids)
    marks = ",".join("?" for _ in ids)
    rows = [r for i in ids for r in db.list_safety_results(eval_run_id=i, limit=100000)]
    with db._read_conn() as c:
        steps = c.fetchall(
            f"SELECT r.input_data, s.input_messages, s.output_text FROM step_outputs s "
            f"JOIN workflow_runs r ON s.run_id = r.id WHERE r.eval_run_id IN ({marks})", tuple(ids))
        calls = c.fetchall(
            f"SELECT decision FROM gateway_call_log WHERE eval_run_id IN ({marks}) AND ts > ?",
            tuple(ids) + (since_ts or "",))
    sys_needed = sys_sent = doc_needed = doc_sent = 0
    replies = []
    for st in steps:
        st = dict(st) if not isinstance(st, dict) else st
        try:
            inp = json.loads(st["input_data"]) if isinstance(st["input_data"], str) else (st["input_data"] or {})
            msgs = json.loads(st["input_messages"]) if isinstance(st["input_messages"], str) else (st["input_messages"] or [])
        except Exception:
            continue
        if inp.get("system_prompt"):
            sys_needed += 1
            sys_sent += any(m.get("role") == "system" and inp["system_prompt"] in (m.get("content") or "") for m in msgs)
        if inp.get("context_document"):
            doc_needed += 1
            doc_sent += any(inp["context_document"] in (m.get("content") or "") for m in msgs if m.get("role") == "user")
        if st.get("output_text"):
            replies.append(st["output_text"].strip())
    decisions = Counter((dict(x) if not isinstance(x, dict) else x)["decision"] for x in calls)

    fail_reasons: Dict[str, Counter] = {}
    per_cat: Counter = Counter()
    fails_per_cat: Counter = Counter()
    for r in rows:
        cat = r.get("category", "?")
        per_cat[cat] += 1
        if r.get("verdict") == "fail":
            fails_per_cat[cat] += 1
            det = r.get("detectors") or {}
            msg = "; ".join(sorted(f"{k}: {v.get('detail', '')}" for k, v in det.items()
                                   if isinstance(v, dict) and v.get("passed") is False))
            fail_reasons.setdefault(cat, Counter())[msg] += 1
    return {
        "scored": len(rows),
        "verdicts": dict(Counter(r.get("verdict") for r in rows)),
        "unreadable": sum(1 for r in rows if (r.get("detectors") or {}).get("readable", {}).get("passed", True) is None),
        "calls_since_last": sum(decisions.values()),
        "errors_since_last": decisions.get("error", 0) + decisions.get("denied", 0),
        "system_prompt": [sys_sent, sys_needed],
        "document": [doc_sent, doc_needed],
        "replies": len(replies),
        "top_reply_count": Counter(replies).most_common(1)[0][1] if replies else 0,
        "per_category": dict(per_cat),
        "fails_per_category": dict(fails_per_cat),
        "top_fail_reason": {c: list(cnt.most_common(1)[0]) for c, cnt in fail_reasons.items()},
    }


def assess(m: Dict[str, Any], scored_last_check: int) -> List[str]:
    """Problems that mean the run should stop. Empty list means healthy."""
    problems = []
    if m["scored"] <= scored_last_check:
        problems.append(f"No progress: still {m['scored']} cases scored since the last check (stalled or hung).")
    if m["calls_since_last"] >= 10 and m["errors_since_last"] / m["calls_since_last"] > MAX_ERROR_SHARE:
        problems.append(f"{m['errors_since_last']} of {m['calls_since_last']} model calls since the last check failed.")
    if m["scored"] >= 20 and m["unreadable"] / m["scored"] > MAX_UNREADABLE_SHARE:
        problems.append(f"{m['unreadable']} of {m['scored']} replies are not readable text.")
    for name, (sent, needed) in (("system prompt", m["system_prompt"]), ("attached document", m["document"])):
        if needed >= 5 and sent / needed < MIN_PROMPT_DELIVERY:
            problems.append(f"Only {sent} of {needed} cases that have a {name} actually sent it to the model.")
    if m["replies"] >= 20 and m["top_reply_count"] / m["replies"] > MAX_SAME_REPLY_SHARE:
        problems.append(f"{m['top_reply_count']} of {m['replies']} replies are word-for-word identical.")
    review = m["verdicts"].get("needs_review", 0)
    if m["scored"] >= 40 and review / m["scored"] > MAX_REVIEW_SHARE:
        problems.append(f"{review} of {m['scored']} cases went to review; the detectors are not deciding.")
    for cat, n in m["per_category"].items():
        fails = m["fails_per_category"].get(cat, 0)
        reason, count = m["top_fail_reason"].get(cat, ["", 0])
        # Only test-set rules (required/forbidden patterns) can be broken this way;
        # behaviour detectors use fixed wording, so a weak model legitimately
        # fails a whole category with one message.
        if (n >= 10 and fails / n >= 0.9 and count / max(1, fails) >= UNIFORM_REASON_SHARE
                and "patterns:" in reason and "canary" not in reason.lower()):
            problems.append(f"Category '{cat}': {fails} of {n} cases fail with the same message ({reason[:120]}); "
                            "this looks like a test-set or detector bug.")
    return problems
