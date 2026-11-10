"""Continuous monitoring execution engine for Forkmark.

Handles:
- Baseline & noise floor establishment
- Scheduled & manual canary test execution
- Drift measurement (divergence, grounding, safety slice, PSI, version change)
- State determination (OK, Watch, Alert, Changed)
- Atomic state updates, revalidation schedule moves, and egress-checked webhook alerts
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import statistics
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from core.egress_policy import check_egress, EgressBlocked
from core.monitors.metrics import (
    calculate_arabic_share,
    classify_psi,
    compute_psi,
    evaluate_divergence,
    evaluate_grounding,
    is_arabic_text,
)
from core.safety.detectors import evaluate_safety_case
from core.safety.thresholds import benjamini_hochberg, fisher_exact_test
from core.suite_runner import run_suite_async

logger = logging.getLogger("forkmark.monitors.engine")


def _percentile(values: List[float], p: float) -> float:
    """Calculate the p-th percentile (0.0 to 1.0) of values."""
    if not values:
        return 0.0
    s = sorted(values)
    idx = int(round(p * (len(s) - 1)))
    return s[min(max(0, idx), len(s) - 1)]


def get_safety_slice_cases() -> List[Dict[str, Any]]:
    """Return a deterministic 30-case sample (10 EN, 10 MSA, 10 Gulf Arabic) of red-team corpus."""
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(base_dir, "fixtures", "safety", "forkmark_redteam_seed_v0_1.json")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        corpus = json.load(f)
    cases = corpus.get("cases", [])
    en_cases = [c for c in cases if c.get("language") == "en"][:10]
    msa_cases = [c for c in cases if c.get("language") == "ar_msa"][:10]
    gulf_cases = [c for c in cases if c.get("language") == "ar_gulf"][:10]
    return en_cases + msa_cases + gulf_cases


def _fetch_test_cases(db: Any, test_set_id: str) -> List[Dict[str, Any]]:
    """Fetch test cases for a test set, normalized as dictionaries."""
    cases = db.list_test_cases(test_set_id) if hasattr(db, "list_test_cases") else []
    result = []
    for tc in cases:
        inp = getattr(tc, "input_data", {})
        inp_str = inp.get("input", "") if isinstance(inp, dict) else str(inp)
        ctx = inp.get("context_document", "") if isinstance(inp, dict) else ""
        result.append({
            "id": getattr(tc, "id", str(uuid.uuid4())),
            "label": getattr(tc, "label", "case"),
            "input_data": inp if isinstance(inp, dict) else {"input": inp_str},
            "input": inp_str,
            "context_document": ctx,
        })
    return result


def _ensure_workflow(db: Any, workflow_name: str = "canary_monitoring") -> str:
    """Ensure the monitoring workflow exists in the database and return its primary key id."""
    wf = db.upsert_workflow(name=workflow_name, description="Continuous canary drift monitoring")
    return wf.id


def _load_step_outputs_for_run(db: Any, eval_run_id: str) -> List[Dict[str, Any]]:
    """Fetch all step outputs for champion branch of an eval run."""
    with db._read_conn() as c:
        rows = c.fetchall(
            """SELECT so.*, wr.test_case_label, b.is_baseline
               FROM step_outputs so
               JOIN workflow_runs wr ON so.run_id = wr.id
               JOIN branches b ON so.branch_id = b.id
               WHERE wr.eval_run_id = ? AND b.is_baseline = 1
               ORDER BY wr.test_case_label ASC""",
            (eval_run_id,),
        )
    return [dict(r) for r in rows]


async def establish_baseline_and_noise_floor(
    db: Any,
    governed_model_id: str,
    connector_id: str,
    test_set_id: str,
    include_safety_slice: bool = True,
    existing_baseline_id: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Establish the baseline eval run and calculate the noise floor for a monitor.

    Runs the canary set back-to-back to establish model self-divergence baseline noise.
    """
    # 1. Fetch test cases
    test_cases = _fetch_test_cases(db, test_set_id)
    if not test_cases:
        # Fallback dummy test case if set empty
        test_cases = [{"id": "tc-canary-default", "label": "canary_default", "input": "Ping model"}]

    if include_safety_slice:
        safety_cases = get_safety_slice_cases()
        # Add safety cases formatted as test cases
        for sc in safety_cases:
            user_p = sc.get("prompt") or (sc.get("turns", [{}])[0].get("content") if sc.get("turns") else "")
            test_cases.append({
                "id": sc["id"],
                "label": f"safety_{sc['id']}",
                "input": user_p,
                "input_data": {"input": user_p},
                "turns": sc.get("turns"),
                "system_prompt": sc.get("system_prompt", ""),
                "context_document": sc.get("context_document", ""),
                "is_safety": True,
                "safety_meta": sc,
            })

    workflow_id = _ensure_workflow(db)
    champion_config = {"connector_id": connector_id, "name": "champion", "temperature": 0.0}

    # Baseline run
    baseline_id = existing_baseline_id
    if not baseline_id:
        baseline_run = db.create_eval_run(
            workflow_id=workflow_id,
            name=f"baseline_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
            branch_a_config=champion_config,
            branch_b_config={},
            test_set_id=test_set_id,
            governed_model_id=governed_model_id,
            run_kind="canary",
        )
        baseline_id = baseline_run.id
        await run_suite_async(
            db=db,
            eval_run_id=baseline_id,
            workflow_id=workflow_id,
            test_cases=test_cases,
            champion=champion_config,
            governed_model_id=governed_model_id,
            run_kind="canary",
        )

    # Second run to establish run-to-run noise floor
    noise_run = db.create_eval_run(
        workflow_id=workflow_id,
        name=f"noise_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
        branch_a_config=champion_config,
        branch_b_config={},
        test_set_id=test_set_id,
        governed_model_id=governed_model_id,
        run_kind="canary",
    )
    await run_suite_async(
        db=db,
        eval_run_id=noise_run.id,
        workflow_id=workflow_id,
        test_cases=test_cases,
        champion=champion_config,
        governed_model_id=governed_model_id,
        run_kind="canary",
    )

    outputs1 = _load_step_outputs_for_run(db, baseline_id)
    outputs2 = _load_step_outputs_for_run(db, noise_run.id)

    # Pair outputs by test_case_label
    out_map2 = {o["test_case_label"]: o["output_text"] for o in outputs2}
    divergences = []
    for o1 in outputs1:
        label = o1["test_case_label"]
        if label in out_map2:
            t1 = o1["output_text"] or ""
            t2 = out_map2[label] or ""
            d = evaluate_divergence(t1, t2)
            divergences.append(d)

    med = statistics.median(divergences) if divergences else 0.0
    p90 = _percentile(divergences, 0.90) if divergences else 0.0

    noise_floor = {
        "divergence_median": round(float(med), 4),
        "divergence_p90": round(float(p90), 4),
        "sample_size": len(divergences),
    }
    return baseline_id, noise_floor


async def send_alert_webhook(url: str, payload: Dict[str, Any]) -> None:
    """Send an HTTP JSON alert webhook, strictly adhering to egress policies."""
    if not url:
        return
    try:
        check_egress(url)
    except Exception as exc:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or url
        logger.warning("Monitor webhook egress blocked by policy for host '%s': %s", host, exc)
        return

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                logger.warning("Alert webhook returned HTTP %d: %s", resp.status_code, resp.text)
    except Exception as exc:
        logger.warning("Error dispatching alert webhook to %s: %s", url, exc)


async def execute_monitor_run(
    db: Any,
    monitor_id: str,
    pre_acquired_lease: bool = False,
    lease_owner: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a single canary monitor run and evaluate drift vs baseline."""
    monitor = db.get_monitor(monitor_id)
    if not monitor:
        raise ValueError(f"Monitor not found: {monitor_id}")

    governed_model_id = monitor["governed_model_id"]
    connector_id = monitor["connector_id"]
    test_set_id = monitor["test_set_id"]
    baseline_eval_run_id = monitor.get("baseline_eval_run_id")
    noise_floor = monitor.get("noise_floor") or {}
    include_safety_slice = monitor.get("include_safety_slice", True)
    owner = lease_owner or f"worker-{uuid.uuid4().hex[:8]}"

    if not pre_acquired_lease:
        claimed = db.acquire_monitor_lease(monitor_id, lease_owner=owner)
        if not claimed:
            logger.info("Could not acquire lease for monitor %s; skipping", monitor_id)
            return {"status": "skipped", "reason": "lease_unavailable"}

    now_iso = datetime.now(timezone.utc).isoformat()
    mrun = db.create_monitor_run({
        "monitor_id": monitor_id,
        "started_at": now_iso,
        "status": "running",
    })
    run_id = mrun["id"]

    try:
        # 1. Ensure baseline and noise floor exist
        if not baseline_eval_run_id or not noise_floor:
            b_id, nf = await establish_baseline_and_noise_floor(
                db, governed_model_id, connector_id, test_set_id, include_safety_slice
            )
            baseline_eval_run_id = b_id
            noise_floor = nf
            db.update_monitor(monitor_id, {
                "baseline_eval_run_id": b_id,
                "noise_floor": nf,
            })

        # 2. Assemble test cases
        test_cases = _fetch_test_cases(db, test_set_id)
        if not test_cases:
            test_cases = [{"id": "tc-canary-default", "label": "canary_default", "input": "Ping model"}]

        safety_cases = get_safety_slice_cases() if include_safety_slice else []
        for sc in safety_cases:
            user_p = sc.get("prompt") or (sc.get("turns", [{}])[0].get("content") if sc.get("turns") else "")
            test_cases.append({
                "id": sc["id"],
                "label": f"safety_{sc['id']}",
                "input": user_p,
                "input_data": {"input": user_p},
                "turns": sc.get("turns"),
                "system_prompt": sc.get("system_prompt", ""),
                "context_document": sc.get("context_document", ""),
                "is_safety": True,
                "safety_meta": sc,
            })

        # 3. Execute current canary run
        workflow_id = _ensure_workflow(db)
        champ_conf = {"connector_id": connector_id, "name": "champion", "temperature": 0.0}
        current_eval_run = db.create_eval_run(
            workflow_id=workflow_id,
            name=f"canary_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
            branch_a_config=champ_conf,
            branch_b_config={},
            test_set_id=test_set_id,
            governed_model_id=governed_model_id,
            run_kind="canary",
        )
        eval_run_id = current_eval_run.id

        await run_suite_async(
            db=db,
            eval_run_id=eval_run_id,
            workflow_id=workflow_id,
            test_cases=test_cases,
            champion={"connector_id": connector_id, "name": "champion", "temperature": 0.0},
            governed_model_id=governed_model_id,
            run_kind="canary",
        )

        # 4. Compare with baseline outputs
        baseline_outputs = _load_step_outputs_for_run(db, baseline_eval_run_id)
        current_outputs = _load_step_outputs_for_run(db, eval_run_id)

        base_by_label = {o["test_case_label"]: o for o in baseline_outputs}
        curr_by_label = {o["test_case_label"]: o for o in current_outputs}

        # Model reported / System fingerprint check
        curr_meta = {}
        for o in current_outputs:
            if o.get("provider_meta"):
                try:
                    curr_meta = json.loads(o["provider_meta"]) if isinstance(o["provider_meta"], str) else o["provider_meta"]
                    if curr_meta.get("model_reported"):
                        break
                except Exception:
                    pass

        base_meta = {}
        for o in baseline_outputs:
            if o.get("provider_meta"):
                try:
                    base_meta = json.loads(o["provider_meta"]) if isinstance(o["provider_meta"], str) else o["provider_meta"]
                    if base_meta.get("model_reported"):
                        break
                except Exception:
                    pass

        curr_model_reported = curr_meta.get("model_reported") or ""
        curr_system_fp = curr_meta.get("system_fingerprint") or ""
        base_model_reported = base_meta.get("model_reported") or ""
        base_system_fp = base_meta.get("system_fingerprint") or ""

        breaches: List[str] = []
        is_changed = False

        if base_model_reported and curr_model_reported and curr_model_reported != base_model_reported:
            breaches.append(
                f"Vendor model changed from '{base_model_reported}' to '{curr_model_reported}'"
            )
            is_changed = True
        if base_system_fp and curr_system_fp and curr_system_fp != base_system_fp:
            breaches.append(
                f"System fingerprint changed from '{base_system_fp}' to '{curr_system_fp}'"
            )
            is_changed = True

        # Metrics computation
        divergences = []
        tokens_base = []
        tokens_curr = []
        ar_shares_base = []
        ar_shares_curr = []
        grounding_base_pass = 0
        grounding_base_fail = 0
        grounding_curr_pass = 0
        grounding_curr_fail = 0
        safety_breaches = []
        latencies = []

        tc_lookup = {tc.get("label"): tc for tc in test_cases}

        for label, curr_o in curr_by_label.items():
            base_o = base_by_label.get(label)
            curr_text = curr_o.get("output_text") or ""
            curr_tok = int(curr_o.get("tokens_output") or len(curr_text.split()))
            tokens_curr.append(curr_tok)
            if curr_o.get("latency_ms"):
                latencies.append(float(curr_o["latency_ms"]))

            tc = tc_lookup.get(label, {})
            user_prompt = tc.get("input") or ""

            # Arabic share
            if is_arabic_text(user_prompt):
                ar_shares_curr.append(calculate_arabic_share(curr_text))

            # Grounding check
            context_doc = tc.get("context_document")
            if context_doc:
                is_faithful = evaluate_grounding(context_doc, curr_text)
                if is_faithful:
                    grounding_curr_pass += 1
                else:
                    grounding_curr_fail += 1

            # Safety slice check
            if tc.get("is_safety") and tc.get("safety_meta"):
                sm = tc["safety_meta"]
                res = evaluate_safety_case(sm, curr_text)
                if res.get("verdict") == "fail":
                    failed_dets = [
                        f"{k}: {v.get('detail')}"
                        for k, v in res.get("detectors", {}).items()
                        if not v.get("passed")
                    ]
                    safety_breaches.append(f"Safety failure in case {label}: {'; '.join(failed_dets)}")

            if base_o:
                base_text = base_o.get("output_text") or ""
                base_tok = int(base_o.get("tokens_output") or len(base_text.split()))
                tokens_base.append(base_tok)

                if is_arabic_text(user_prompt):
                    ar_shares_base.append(calculate_arabic_share(base_text))

                if context_doc:
                    is_faithful_b = evaluate_grounding(context_doc, base_text)
                    if is_faithful_b:
                        grounding_base_pass += 1
                    else:
                        grounding_base_fail += 1

                div = evaluate_divergence(base_text, curr_text)
                divergences.append(div)

        # Summarize Metrics
        div_med = statistics.median(divergences) if divergences else 0.0
        div_p90 = _percentile(divergences, 0.90) if divergences else 0.0
        psi_len = compute_psi(tokens_base, tokens_curr)
        psi_arabic = compute_psi(ar_shares_base, ar_shares_curr) if ar_shares_base else 0.0
        p95_lat = _percentile(latencies, 0.95) if latencies else 0.0

        metrics = {
            "divergence_median": round(float(div_med), 4),
            "divergence_p90": round(float(div_p90), 4),
            "noise_floor_p90": noise_floor.get("divergence_p90", 0.0),
            "psi_reply_length": psi_len,
            "psi_arabic_share": psi_arabic,
            "latency_p95_ms": round(float(p95_lat), 2),
            "grounding_curr_pass_rate": (
                round(grounding_curr_pass / (grounding_curr_pass + grounding_curr_fail), 4)
                if (grounding_curr_pass + grounding_curr_fail) > 0
                else None
            ),
            "safety_failures": len(safety_breaches),
        }

        # Drift Alert / Watch Rules
        # Rule 1: Divergence vs Noise floor
        nf_p90 = float(noise_floor.get("divergence_p90", 0.0))
        # Check consecutive runs from history
        prior_runs = db.list_monitor_runs(monitor_id, limit=2)
        prior_elevated = False
        if prior_runs:
            prior_m = prior_runs[0].get("metrics") or {}
            if float(prior_m.get("divergence_median", 0.0)) > nf_p90:
                prior_elevated = True

        if nf_p90 > 0 and div_med > nf_p90:
            if prior_elevated:
                breaches.append(
                    f"Behavioural divergence median ({div_med:.3f}) exceeded noise floor p90 ({nf_p90:.3f}) in two consecutive runs"
                )
            else:
                breaches.append(
                    f"Watch: Behavioural divergence median ({div_med:.3f}) exceeded noise floor p90 ({nf_p90:.3f})"
                )

        # Rule 2: Grounding drop (Fisher exact test BH p < 0.01 and drop >= 5 points)
        if (grounding_base_pass + grounding_base_fail) > 0 and (grounding_curr_pass + grounding_curr_fail) > 0:
            base_rate = grounding_base_pass / (grounding_base_pass + grounding_base_fail)
            curr_rate = grounding_curr_pass / (grounding_curr_pass + grounding_curr_fail)
            drop = base_rate - curr_rate
            if drop >= 0.05:
                table = [
                    [grounding_curr_pass, grounding_curr_fail],
                    [grounding_base_pass, grounding_base_fail],
                ]
                p_val = fisher_exact_test(table)
                is_sig = benjamini_hochberg([p_val], fdr=0.01)[0]
                if is_sig:
                    breaches.append(
                        f"Grounding pass rate dropped by {drop * 100:.1f} percentage points (p={p_val:.4f} < 0.01)"
                    )

        # Rule 3: Safety breaches
        for sb in safety_breaches:
            breaches.append(sb)

        # Rule 4: Reply length PSI
        psi_len_band = classify_psi(psi_len)
        if psi_len_band == "Alert":
            breaches.append(f"Reply length PSI ({psi_len:.3f}) >= 0.25 (Alert)")
        elif psi_len_band == "Watch":
            breaches.append(f"Reply length PSI ({psi_len:.3f}) is in watch band (0.10 - 0.25)")

        # Rule 5: Arabic share PSI
        psi_ar_band = classify_psi(psi_arabic)
        if psi_ar_band == "Alert":
            breaches.append(f"Arabic script share PSI ({psi_arabic:.3f}) >= 0.25 (Alert)")
        elif psi_ar_band == "Watch":
            breaches.append(f"Arabic script share PSI ({psi_arabic:.3f}) is in watch band (0.10 - 0.25)")

        # State determination
        if is_changed:
            state = "Changed"
        elif any("Alert" in b or "dropped by" in b or "Safety failure" in b or "consecutive runs" in b for b in breaches):
            state = "Alert"
        elif any("Watch" in b or "watch band" in b for b in breaches):
            state = "Watch"
        else:
            state = "OK"

        finished_iso = datetime.now(timezone.utc).isoformat()
        db.update_monitor_run(run_id, {
            "eval_run_id": eval_run_id,
            "finished_at": finished_iso,
            "status": "completed",
            "state": state,
            "metrics": metrics,
            "breaches": breaches,
            "model_reported": curr_model_reported,
            "system_fingerprint": curr_system_fp,
        })

        # Atomic mutation: state, next_validation_due if Changed, and supervisory audit log
        now_utc = datetime.now(timezone.utc)
        next_due = now_utc.isoformat() if state == "Changed" else None
        audit_action = "model.vendor_change_detected" if state == "Changed" else (
            "monitor.alert" if state in ("Alert", "Watch") else "monitor.run_completed"
        )
        audit_entry = {
            "action": audit_action,
            "actor": "system",
            "actor_role": "system",
            "detail": {
                "monitor_id": monitor_id,
                "monitor_run_id": run_id,
                "eval_run_id": eval_run_id,
                "state": state,
                "breaches": breaches,
                "metrics": metrics,
            },
        }
        db.update_monitor_and_model_state_atomic(
            monitor_id=monitor_id,
            state=state,
            governed_model_id=governed_model_id,
            audit_entry=audit_entry,
            next_validation_due=next_due,
        )

        if governed_model_id:
            try:
                from core.model_inventory import ModelInventory
                inv = ModelInventory(db)
                m = inv.get_model(governed_model_id)
                if m:
                    arts = set(m.present_artifacts)
                    if "ongoing_monitoring_report" not in arts:
                        arts.add("ongoing_monitoring_report")
                        inv.update_model(
                            governed_model_id,
                            present_artifacts=list(arts),
                            audit_actor="system",
                            audit_role="system",
                        )
            except Exception as e:
                logger.warning("Failed to update model present_artifacts: %s", e)

        # Webhook alert if configured
        webhook_url = os.getenv("FM_ALERT_WEBHOOK_URL", "").strip()
        if webhook_url and state in ("Alert", "Changed"):
            await send_alert_webhook(webhook_url, {
                "event": audit_action,
                "monitor_id": monitor_id,
                "governed_model_id": governed_model_id,
                "state": state,
                "breaches": breaches,
                "metrics": metrics,
                "timestamp": finished_iso,
            })

        # Advance monitor schedule & release lease
        interval_h = int(monitor.get("interval_hours", 24))
        next_run = (datetime.now(timezone.utc) + timedelta(hours=interval_h)).isoformat()
        db.release_monitor_lease(
            monitor_id=monitor_id,
            lease_owner=owner,
            next_run_at=next_run,
            last_run_at=finished_iso,
            state=state,
        )

        return db.get_monitor_run(run_id) or {}

    except Exception as exc:
        logger.exception("Error executing monitor %s: %s", monitor_id, exc)
        db.update_monitor_run(run_id, {
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": "failed",
            "breaches": [f"Execution error: {str(exc)}"],
        })
        # Release lease on error
        db.release_monitor_lease(monitor_id=monitor_id, lease_owner=owner)
        raise
