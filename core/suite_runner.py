"""Execution engine for test suites, validation runs, and canary monitoring.

Centralizes test case execution through the LLM gateway, supporting:
- 'ab' (champion vs challenger)
- 'safety' (red-team attack corpus)
- 'stress' (adversarial/formatting perturbations)
- 'canary' (drift and continuous monitoring)
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from core.models import EvalRunStatus
from core.llm_gateway import gateway

logger = logging.getLogger("forkmark.suite_runner")


def _resolve_messages(test_case: Dict[str, Any], system_prompt: Optional[str] = None) -> List[Dict[str, str]]:
    """Construct chat messages from a test case dict or TestCase instance."""
    # Check for multi-turn or explicit turns
    if "turns" in test_case and test_case["turns"]:
        messages = []
        if system_prompt or test_case.get("system_prompt"):
            sp = system_prompt or test_case.get("system_prompt")
            messages.append({"role": "system", "content": sp})
        messages.extend(test_case["turns"])
        return messages

    # Context document (e.g. for grounding or red-team cases)
    context_doc = test_case.get("context_document") or ""
    user_input = (
        test_case.get("input")
        or test_case.get("input_data", {}).get("input")
        or test_case.get("prompt")
        or ""
    )
    if not user_input and isinstance(test_case.get("input_data"), str):
        user_input = test_case["input_data"]

    content = f"Context:\n{context_doc}\n\nQuestion:\n{user_input}" if context_doc else user_input

    messages = []
    sp = system_prompt or test_case.get("system_prompt")
    if sp:
        messages.append({"role": "system", "content": sp})
    messages.append({"role": "user", "content": content})
    return messages


async def run_suite_async(
    db: Any,
    eval_run_id: str,
    workflow_id: str,
    test_cases: List[Dict[str, Any]],
    champion: Dict[str, Any],
    challenger: Optional[Dict[str, Any]] = None,
    governed_model_id: Optional[str] = None,
    run_kind: str = "ab",
    evaluators: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Execute test cases through the gateway and record results and provenance."""
    evaluators = evaluators or []
    errors: List[str] = []
    fingerprint_recorded = False

    # Mark eval run as running
    await asyncio.to_thread(
        db.update_eval_run_status, eval_run_id, EvalRunStatus.RUNNING, total_cases=len(test_cases)
    )

    for tc_idx, tc in enumerate(test_cases):
        label = tc.get("label") or tc.get("id") or "test_case"
        tc_input = tc.get("input_data") or {"input": tc.get("input", "")}
        if not isinstance(tc_input, dict):
            tc_input = {"input": str(tc_input)}

        run = await asyncio.to_thread(
            db.create_run,
            workflow_id=workflow_id,
            input_data=tc_input,
            eval_run_id=eval_run_id,
            test_case_label=label,
        )

        champ_conn_id = champion.get("connector_id") or champion.get("provider_id")
        champ_model = champion.get("model") or champion.get("model_id") or "default"
        champ_temp = float(champion.get("temperature", 0.0))
        champ_sys_prompt = champion.get("system_prompt")

        branch_a_obj = await asyncio.to_thread(
            db.create_branch,
            run.id,
            workflow_id,
            champion.get("name", "champion"),
            champ_model,
            is_baseline=True,
            provider_id=champ_conn_id,
        )

        branch_b_obj = None
        if challenger:
            chal_conn_id = challenger.get("connector_id") or challenger.get("provider_id")
            chal_model = challenger.get("model") or challenger.get("model_id") or "default"
            chal_temp = float(challenger.get("temperature", 0.0))
            chal_sys_prompt = challenger.get("system_prompt")
            branch_b_obj = await asyncio.to_thread(
                db.create_branch,
                run.id,
                workflow_id,
                challenger.get("name", "challenger"),
                chal_model,
                is_baseline=False,
                provider_id=chal_conn_id,
            )

        is_stress = (run_kind == "stress")
        pert_variant = None
        if is_stress:
            from core.safety.stress_engine import (
                generate_stress_variants,
                compute_decision_flip_rate,
                compute_score_stability,
                compute_figure_consistency,
                compute_bilingual_figure_match,
            )
            user_text = (
                tc.get("input")
                or (tc.get("input_data", {}).get("input") if isinstance(tc.get("input_data"), dict) else "")
                or tc.get("prompt")
                or ""
            )
            if not user_text and isinstance(tc.get("input_data"), str):
                user_text = tc["input_data"]
            variants = generate_stress_variants(user_text, seed=tc_idx + 1)
            pert_variant = variants[1 + (tc_idx % (len(variants) - 1))]
            pert_tc = dict(tc)
            pert_tc["input"] = pert_variant["text"]
            if isinstance(pert_tc.get("input_data"), dict):
                pert_tc["input_data"] = dict(pert_tc["input_data"], input=pert_variant["text"])
            messages_a = _resolve_messages(pert_tc, champ_sys_prompt)
        else:
            messages_a = _resolve_messages(tc, champ_sys_prompt)

        # 1. Execute Champion
        try:
            resp_a_base = None
            if is_stress:
                # Execute original baseline case to establish ground-truth decision for comparison
                resp_a_base = await gateway.achat(
                    connector_id=champ_conn_id,
                    messages=_resolve_messages(tc, champ_sys_prompt),
                    model=champ_model,
                    temperature=champ_temp,
                    purpose="validation_stress_baseline",
                    eval_run_id=eval_run_id,
                )

            resp_a = await gateway.achat(
                connector_id=champ_conn_id,
                messages=messages_a,
                model=champ_model,
                temperature=champ_temp,
                purpose=f"validation_{run_kind}",
                eval_run_id=eval_run_id,
            )
            meta_a = {
                "model_reported": resp_a.model_reported,
                "system_fingerprint": resp_a.system_fingerprint,
                "finish_reason": resp_a.finish_reason,
                "cross_border": resp_a.cross_border,
            }
            await asyncio.to_thread(
                db.save_step_output,
                run_id=run.id,
                branch_id=branch_a_obj.id,
                step_name="step_1",
                step_index=0,
                input_messages=messages_a,
                output_text=resp_a.text,
                model_id=champ_model,
                temperature=champ_temp,
                tokens_input=resp_a.tokens_in,
                tokens_output=resp_a.tokens_out,
                latency_ms=resp_a.latency_ms,
                provider_meta=meta_a,
            )

            if governed_model_id and resp_a.model_reported and not fingerprint_recorded:
                try:
                    await asyncio.to_thread(
                        db.record_fingerprint,
                        governed_model_id=governed_model_id,
                        connector_id=champ_conn_id or "",
                        model_reported=resp_a.model_reported,
                        system_fingerprint=resp_a.system_fingerprint,
                        source="validation_run",
                        eval_run_id=eval_run_id,
                    )
                    fingerprint_recorded = True
                except Exception as fe:
                    logger.warning("Failed to record model fingerprint: %s", fe)

            # Evaluate safety / stress detectors if applicable
            if run_kind in ("safety", "stress"):
                from core.safety.detectors import evaluate_safety_case
                case_dict = dict(tc.get("input_data")) if isinstance(tc.get("input_data"), dict) else {}
                for k, v in tc.items():
                    if k not in case_dict:
                        case_dict[k] = v
                eval_res = evaluate_safety_case(case_dict, resp_a.text)
                if is_stress and pert_variant and resp_a_base:
                    eval_base = evaluate_safety_case(case_dict, resp_a_base.text)
                    base_v = eval_base["verdict"]
                    pert_v = eval_res["verdict"]
                    flip_rate = compute_decision_flip_rate(base_v, [pert_v])
                    is_flipped = (flip_rate > 0.0)
                    stab_cv = compute_score_stability([
                        1.0 if base_v == "pass" else 0.0,
                        1.0 if pert_v == "pass" else 0.0,
                    ])
                    is_translation = (pert_variant.get("variant_type") in ("arabic_translation", "translation"))
                    is_fig_match, fig_ratio, _, _ = compute_figure_consistency(
                        resp_a_base.text, resp_a.text
                    )
                    eval_res["detectors"]["stress"] = {
                        "flipped": is_flipped,
                        "base_verdict": base_v,
                        "variant_verdict": pert_v,
                        "variant_type": pert_variant["variant_type"],
                        "stability_cv": stab_cv,
                        "figure_consistency_match": is_fig_match,
                        "figure_consistency_ratio": fig_ratio,
                        "bilingual_figure_match": is_fig_match,
                        "bilingual_match_ratio": fig_ratio,
                        "is_translation_variant": is_translation,
                    }
                await asyncio.to_thread(
                    db.save_safety_result,
                    eval_run_id=eval_run_id,
                    governed_model_id=governed_model_id,
                    test_case_id=tc.get("id") or label,
                    case_key=label,
                    branch="champion",
                    category=case_dict.get("category", "unknown"),
                    language=case_dict.get("language", "en"),
                    severity=case_dict.get("severity", "HIGH"),
                    expected=case_dict.get("expected", "refuse"),
                    verdict=eval_res["verdict"],
                    detectors=eval_res["detectors"],
                    reply_excerpt=eval_res["reply_excerpt"],
                )

        except Exception as exc:
            errors.append(f"[{label}/champion] {exc}")
            resp_a = None

        # 2. Execute Challenger if present
        if challenger and branch_b_obj:
            resp_b_base = None
            if is_stress and pert_variant:
                pert_tc_b = dict(tc)
                pert_tc_b["input"] = pert_variant["text"]
                if isinstance(pert_tc_b.get("input_data"), dict):
                    pert_tc_b["input_data"] = dict(pert_tc_b["input_data"], input=pert_variant["text"])
                messages_b = _resolve_messages(pert_tc_b, chal_sys_prompt)
            else:
                messages_b = _resolve_messages(tc, chal_sys_prompt)
            try:
                if is_stress:
                    resp_b_base = await gateway.achat(
                        connector_id=chal_conn_id,
                        messages=_resolve_messages(tc, chal_sys_prompt),
                        model=chal_model,
                        temperature=chal_temp,
                        purpose="validation_stress_baseline",
                        eval_run_id=eval_run_id,
                    )

                resp_b = await gateway.achat(
                    connector_id=chal_conn_id,
                    messages=messages_b,
                    model=chal_model,
                    temperature=chal_temp,
                    purpose=f"validation_{run_kind}",
                    eval_run_id=eval_run_id,
                )
                meta_b = {
                    "model_reported": resp_b.model_reported,
                    "system_fingerprint": resp_b.system_fingerprint,
                    "finish_reason": resp_b.finish_reason,
                    "cross_border": resp_b.cross_border,
                }
                await asyncio.to_thread(
                    db.save_step_output,
                    run_id=run.id,
                    branch_id=branch_b_obj.id,
                    step_name="step_1",
                    step_index=0,
                    input_messages=messages_b,
                    output_text=resp_b.text,
                    model_id=chal_model,
                    temperature=chal_temp,
                    tokens_input=resp_b.tokens_in,
                    tokens_output=resp_b.tokens_out,
                    latency_ms=resp_b.latency_ms,
                    provider_meta=meta_b,
                )

                if run_kind in ("safety", "stress"):
                    from core.safety.detectors import evaluate_safety_case
                    case_dict = dict(tc.get("input_data")) if isinstance(tc.get("input_data"), dict) else {}
                    for k, v in tc.items():
                        if k not in case_dict:
                            case_dict[k] = v
                    eval_res_b = evaluate_safety_case(case_dict, resp_b.text)
                    if is_stress and pert_variant and resp_b_base:
                        eval_b_base = evaluate_safety_case(case_dict, resp_b_base.text)
                        base_vb = eval_b_base["verdict"]
                        pert_vb = eval_res_b["verdict"]
                        flip_rate_b = compute_decision_flip_rate(base_vb, [pert_vb])
                        is_flipped_b = (flip_rate_b > 0.0)
                        stab_cv_b = compute_score_stability([
                            1.0 if base_vb == "pass" else 0.0,
                            1.0 if pert_vb == "pass" else 0.0,
                        ])
                        is_translation = (pert_variant.get("variant_type") in ("arabic_translation", "translation"))
                        is_fig_match_b, fig_ratio_b, _, _ = compute_figure_consistency(
                            resp_b_base.text, resp_b.text
                        )
                        eval_res_b["detectors"]["stress"] = {
                            "flipped": is_flipped_b,
                            "base_verdict": base_vb,
                            "variant_verdict": pert_vb,
                            "variant_type": pert_variant["variant_type"],
                            "stability_cv": stab_cv_b,
                            "figure_consistency_match": is_fig_match_b,
                            "figure_consistency_ratio": fig_ratio_b,
                            "bilingual_figure_match": is_fig_match_b,
                            "bilingual_match_ratio": fig_ratio_b,
                            "is_translation_variant": is_translation,
                        }
                    await asyncio.to_thread(
                        db.save_safety_result,
                        eval_run_id=eval_run_id,
                        governed_model_id=governed_model_id,
                        test_case_id=tc.get("id") or label,
                        case_key=label,
                        branch="challenger",
                        category=case_dict.get("category", "unknown"),
                        language=case_dict.get("language", "en"),
                        severity=case_dict.get("severity", "HIGH"),
                        expected=case_dict.get("expected", "refuse"),
                        verdict=eval_res_b["verdict"],
                        detectors=eval_res_b["detectors"],
                        reply_excerpt=eval_res_b["reply_excerpt"],
                    )
            except Exception as exc:
                errors.append(f"[{label}/challenger] {exc}")
                resp_b = None

            # Create comparison and enqueue scoring
            comp = await asyncio.to_thread(
                db.create_comparison,
                run_id=run.id,
                workflow_id=workflow_id,
                branch_a_id=branch_a_obj.id,
                branch_b_id=branch_b_obj.id,
                step_names=["step_1"],
                eval_run_id=eval_run_id,
                test_case_label=label,
                scoring_status="pending",
            )
            from core.background import async_enqueue_scoring

            asyncio.create_task(
                async_enqueue_scoring(
                    db,
                    comp.id,
                    branch_a_obj.id,
                    branch_b_obj.id,
                    evaluators,
                )
            )

        await asyncio.to_thread(db.complete_run, run.id)

    error_count = len(errors)
    total_count = len(test_cases)
    error_rate = error_count / max(1, total_count)
    if error_count == 0:
        status = EvalRunStatus.COMPLETED
    elif error_rate < 0.5:
        status = EvalRunStatus.COMPLETED_WITH_ERRORS
    else:
        status = EvalRunStatus.FAILED

    await asyncio.to_thread(
        db.update_eval_run_status, eval_run_id, status, total_cases=len(test_cases)
    )

    if governed_model_id and run_kind in ("safety", "stress"):
        if error_rate < 0.05:
            try:
                from core.model_inventory import ModelInventory
                inv = ModelInventory(db)
                m = inv.get_model(governed_model_id)
                if m:
                    artifacts = set(m.present_artifacts)
                    if "robustness_stress_test" not in artifacts:
                        artifacts.add("robustness_stress_test")
                        inv.update_model(
                            governed_model_id,
                            present_artifacts=list(artifacts),
                            audit_actor="system",
                            audit_role="system",
                        )
            except Exception as e:
                logger.warning("Failed to update model present_artifacts: %s", e)

    return {"eval_run_id": eval_run_id, "status": status.value, "errors": errors}
