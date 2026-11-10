"""No-code workflow runner and prompt playground endpoints."""
from __future__ import annotations

import asyncio
import os
import time
from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel, Field
from typing import Optional, List

from backend.response_models import RunnerResponse
from backend.deps import (
    db, ui_write_auth, divergence_score, summarize_divergence, async_enqueue_scoring,
)
from core.models import EvalRunStatus

router = APIRouter(prefix="/api", tags=["runner"])

# Strong references for background scoring tasks to prevent premature Python GC
_BACKGROUND_TASKS: set[asyncio.Task] = set()


# ── Runner schemas ───────────────────────────────────────────────────────────

class RunnerStepConfig(BaseModel):
    name: str
    system_prompt: str
    user_prompt_template: str

class RunnerBranchConfig(BaseModel):
    name: str
    model_id: str
    temperature: float = 0.7
    steps: List[RunnerStepConfig]
    provider_id: Optional[str] = None

class RunnerTestCase(BaseModel):
    label: str
    input: str

class RunnerBody(BaseModel):
    workflow_name: str
    eval_run_name: str
    branch_a: RunnerBranchConfig
    branch_b: RunnerBranchConfig
    test_cases: List[RunnerTestCase]


# ── Playground schemas ───────────────────────────────────────────────────────

class PlaygroundRequest(BaseModel):
    prompt: str
    model_a: str = "gpt-4o-mini"
    model_b: str = "gpt-4o"
    system_prompt: Optional[str] = None
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=1, le=16384)
    # Optional per-model overrides (fall back to temperature/max_tokens above)
    temperature_a: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    temperature_b: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    max_tokens_a: Optional[int] = Field(default=None, ge=1, le=16384)
    max_tokens_b: Optional[int] = Field(default=None, ge=1, le=16384)
    workflow_id: Optional[str] = None
    provider_id_a: Optional[str] = None
    provider_id_b: Optional[str] = None


# ── Provider credential resolution ──────────────────────────────────────────

def _resolve_credentials(provider_id: Optional[str] = None) -> tuple:
    """Resolve LLM credentials from provider_id, default provider, or legacy settings.

    Returns (api_key, base_url). Raises HTTPException if no credentials found.
    """
    if provider_id:
        creds = db.get_provider_credentials(provider_id)
        if creds is None:
            raise HTTPException(400, f"Provider '{provider_id}' not found")
        if not creds["api_key"]:
            raise HTTPException(400, f"Provider '{provider_id}' has no API key configured")
        return creds["api_key"], creds["base_url"]

    # Try default provider
    default = db.get_default_provider_credentials()
    if default and default.get("api_key"):
        return default["api_key"], default.get("base_url", "")

    # Fall back to legacy settings
    api_key = (db.get_setting("openai_api_key")
               or os.getenv("OPENAI_API_KEY")
               or os.getenv("FM_OPENAI_API_KEY"))
    base_url = db.get_setting("openai_base_url") or ""
    if not api_key:
        raise HTTPException(400, "No LLM provider configured. Add one in Settings → Providers.")
    return api_key, base_url


# ── LLM call helper ─────────────────────────────────────────────────────────

def _call_llm(model_id: str, temperature: float, messages: list,
              provider_id: Optional[str] = None, eval_run_id: Optional[str] = None) -> dict:
    from core.llm_gateway import gateway
    resp = gateway.chat(
        connector_id=provider_id,
        messages=messages,
        model=model_id,
        temperature=temperature,
        purpose="runner",
        eval_run_id=eval_run_id,
    )
    return {
        "text": resp.text,
        "tokens_input": resp.tokens_in,
        "tokens_output": resp.tokens_out,
        "latency_ms": resp.latency_ms,
        "model_reported": resp.model_reported,
        "system_fingerprint": resp.system_fingerprint,
        "finish_reason": resp.finish_reason,
        "cross_border": resp.cross_border,
    }


# ── Runner endpoint ──────────────────────────────────────────────────────────

@router.post("/runner", status_code=201, response_model=RunnerResponse)
async def run_workflow_nocode(body: RunnerBody, _auth=Depends(ui_write_auth)):
    import concurrent.futures as _cf
    from core.llm_gateway import gateway
    from core.egress_policy import EgressBlocked

    # 1. Resolve credentials per branch (validates provider existence and API keys first)
    key_a, url_a = _resolve_credentials(body.branch_a.provider_id)
    key_b, url_b = _resolve_credentials(body.branch_b.provider_id)

    # 2. Egress pre-validation: check both branches through the gateway
    try:
        conn_a = gateway._resolve_connection(body.branch_a.provider_id)
        gateway._check_and_log_egress(conn_a, body.branch_a.model_id, "runner", None, "system")
        conn_b = gateway._resolve_connection(body.branch_b.provider_id)
        gateway._check_and_log_egress(conn_b, body.branch_b.model_id, "runner", None, "system")
    except EgressBlocked as e:
        raise HTTPException(403, str(e))

    if not body.test_cases:
        raise HTTPException(400, "test_cases must not be empty")

    wf = await asyncio.to_thread(db.upsert_workflow, body.workflow_name)
    er = await asyncio.to_thread(
        db.create_eval_run,
        workflow_id=wf.id, name=body.eval_run_name,
        description=f"No-code runner: {body.branch_a.model_id} vs {body.branch_b.model_id}",
        branch_a_config={"name": body.branch_a.name, "model_id": body.branch_a.model_id,
                         **({"provider_id": body.branch_a.provider_id} if body.branch_a.provider_id else {})},
        branch_b_config={"name": body.branch_b.name, "model_id": body.branch_b.model_id,
                         **({"provider_id": body.branch_b.provider_id} if body.branch_b.provider_id else {})},
    )

    errors: list[str] = []

    for tc in body.test_cases:
        run = await asyncio.to_thread(
            db.create_run,
            workflow_id=wf.id,
            input_data={"input": tc.input, "label": tc.label},
            eval_run_id=er.id, test_case_label=tc.label,
        )
        branch_a_obj = await asyncio.to_thread(db.create_branch, run.id, wf.id, body.branch_a.name, body.branch_a.model_id)
        branch_b_obj = await asyncio.to_thread(db.create_branch, run.id, wf.id, body.branch_b.name, body.branch_b.model_id)

        def _run_branch(branch_cfg, branch_obj):
            context_messages: list[dict] = []
            for idx, step in enumerate(branch_cfg.steps):
                user_content = step.user_prompt_template.replace("{{input}}", tc.input)
                messages = (
                    [{"role": "system", "content": step.system_prompt}] if step.system_prompt else []
                ) + context_messages + [{"role": "user", "content": user_content}]
                try:
                    result = _call_llm(branch_cfg.model_id, branch_cfg.temperature, messages, branch_cfg.provider_id, er.id)
                    db.save_step_output(
                        run_id=run.id, branch_id=branch_obj.id,
                        step_name=step.name, step_index=idx,
                        input_messages=messages, output_text=result["text"],
                        model_id=branch_cfg.model_id, temperature=branch_cfg.temperature,
                        tokens_input=result["tokens_input"], tokens_output=result["tokens_output"],
                        latency_ms=result["latency_ms"],
                        provider_meta={
                            "model_reported": result.get("model_reported", ""),
                            "system_fingerprint": result.get("system_fingerprint", ""),
                            "finish_reason": result.get("finish_reason", "stop"),
                            "cross_border": result.get("cross_border", 0),
                        },
                    )
                    context_messages.append({"role": "assistant", "content": result["text"]})
                except Exception as exc:
                    errors.append(f"[{tc.label}/{branch_cfg.name}/{step.name}] {exc}")

        # Execute both branches concurrently across thread pool
        loop = asyncio.get_running_loop()
        await asyncio.gather(
            loop.run_in_executor(None, _run_branch, body.branch_a, branch_a_obj),
            loop.run_in_executor(None, _run_branch, body.branch_b, branch_b_obj),
        )

        step_names = [s.name for s in body.branch_a.steps]
        comp = await asyncio.to_thread(
            db.create_comparison,
            run_id=run.id, workflow_id=wf.id,
            branch_a_id=branch_a_obj.id, branch_b_id=branch_b_obj.id,
            step_names=step_names, eval_run_id=er.id,
            test_case_label=tc.label, scoring_status="pending",
        )
        
        # Enqueue async scoring directly with a retained reference to prevent premature GC
        score_task = asyncio.create_task(
            async_enqueue_scoring(db, comp.id, branch_a_obj.id, branch_b_obj.id, [])
        )
        _BACKGROUND_TASKS.add(score_task)
        score_task.add_done_callback(_BACKGROUND_TASKS.discard)
        await asyncio.to_thread(db.complete_run, run.id)

    await asyncio.to_thread(
        db.update_eval_run_status, er.id, EvalRunStatus.COMPLETED, total_cases=len(body.test_cases)
    )
    return {"eval_run_id": er.id, "errors": errors}


# ── Playground endpoint ──────────────────────────────────────────────────────

@router.post("/playground")
async def playground_run(body: PlaygroundRequest, background: BackgroundTasks,
                         _auth=Depends(ui_write_auth)):
    import httpx

    # Resolve credentials per model
    key_a, url_a = _resolve_credentials(body.provider_id_a)
    key_b, url_b = _resolve_credentials(body.provider_id_b)

    messages = []
    if body.system_prompt:
        messages.append({"role": "system", "content": body.system_prompt})
    messages.append({"role": "user", "content": body.prompt})

    # Per-model temperature / max_tokens (fall back to the shared values)
    temp_a = body.temperature_a if body.temperature_a is not None else body.temperature
    temp_b = body.temperature_b if body.temperature_b is not None else body.temperature
    max_a = body.max_tokens_a if body.max_tokens_a is not None else body.max_tokens
    max_b = body.max_tokens_b if body.max_tokens_b is not None else body.max_tokens

    wf = db.upsert_workflow("Playground", description="Interactive prompt playground")

    from core.llm_gateway import gateway
    from core.egress_policy import EgressBlocked

    # Egress preflight validation
    try:
        conn_a = gateway._resolve_connection(body.provider_id_a)
        gateway._check_and_log_egress(conn_a, body.model_a, "playground", None, "system")
        conn_b = gateway._resolve_connection(body.provider_id_b)
        gateway._check_and_log_egress(conn_b, body.model_b, "playground", None, "system")
    except EgressBlocked as e:
        raise HTTPException(403, str(e))

    async def _call_model(model: str, provider_id: Optional[str],
                          temperature: float, max_tokens: int):
        resp = await gateway.achat(
            connector_id=provider_id,
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            purpose="playground",
        )
        return {
            "output_text": resp.text,
            "model_id": resp.model_reported,
            "latency_ms": resp.latency_ms,
            "tokens_input": resp.tokens_in,
            "tokens_output": resp.tokens_out,
            "finish_reason": resp.finish_reason,
        }

    try:
        result_a, result_b = await asyncio.gather(
            _call_model(body.model_a, body.provider_id_a, temp_a, max_a),
            _call_model(body.model_b, body.provider_id_b, temp_b, max_b),
        )
    except EgressBlocked as e:
        raise HTTPException(403, str(e))
    except Exception as e:
        raise HTTPException(502, f"Model call failed: {str(e)[:200]}")

    run = db.create_run(
        workflow_id=wf.id,
        input_data={"prompt": body.prompt, "system_prompt": body.system_prompt},
        metadata={"source": "playground"},
    )
    branch_a = db.create_branch(run_id=run.id, workflow_id=wf.id, name="Branch A", model_id=body.model_a, temperature=temp_a)
    branch_b = db.create_branch(run_id=run.id, workflow_id=wf.id, name="Branch B", model_id=body.model_b, temperature=temp_b)

    db.save_step_output(run_id=run.id, branch_id=branch_a.id, step_name="answer", step_index=0,
                        input_messages=messages, output_text=result_a["output_text"],
                        model_id=body.model_a, temperature=temp_a,
                        tokens_input=result_a["tokens_input"], tokens_output=result_a["tokens_output"],
                        latency_ms=result_a["latency_ms"])
    db.save_step_output(run_id=run.id, branch_id=branch_b.id, step_name="answer", step_index=0,
                        input_messages=messages, output_text=result_b["output_text"],
                        model_id=body.model_b, temperature=temp_b,
                        tokens_input=result_b["tokens_input"], tokens_output=result_b["tokens_output"],
                        latency_ms=result_b["latency_ms"])

    db.complete_run(run.id)
    div_score = divergence_score(result_a["output_text"], result_b["output_text"])
    summary = summarize_divergence(result_a["output_text"], result_b["output_text"], div_score)

    comp = db.create_comparison(run_id=run.id, workflow_id=wf.id,
                                branch_a_id=branch_a.id, branch_b_id=branch_b.id,
                                step_names=["answer"], scoring_status="completed")
    db.update_comparison_scoring(comp.id, divergence_score=div_score, scoring_status="completed")

    return {
        "comparison_id": comp.id, "run_id": run.id,
        "model_a": result_a, "model_b": result_b,
        "divergence_score": div_score, "divergence_summary": summary,
    }
