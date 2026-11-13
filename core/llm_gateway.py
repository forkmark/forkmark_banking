"""Centralized Model Gateway with sovereign egress enforcement and telemetry logging.

Every outbound model call across ForkMark passes through ModelGateway.
Enforces:
- Sovereign data residency egress policy (pre-socket validation)
- Cross-border inference detection (§5(a), §9(c))
- Concurrency limiting per connection (FM_CONNECTOR_MAX_CONCURRENCY)
- Transient retries with exponential backoff and 429 Retry-After support
- Audit logging to gateway_call_log (migration v16)
- Development response caching (FM_DEV_RESPONSE_CACHE)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from core.egress_policy import EgressBlocked, check_egress, is_internal_host, is_external_endpoint
from core.connectors.openai_compatible import OpenAICompatibleConnector
from core.connectors.azure_openai import AzureOpenAIConnector

logger = logging.getLogger("forkmark.gateway")


@dataclass(frozen=True)
class GatewayResponse:
    text: str
    model_reported: str          # the `model` field the endpoint returned
    system_fingerprint: str      # OpenAI/Azure `system_fingerprint`, "" if absent
    finish_reason: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    connector_id: str
    inference_region: str        # from the connector record
    cross_border: bool           # True if the call left FM_DEFAULT_REGION



class RateBudgetExhausted(RuntimeError):
    """The connection's daily request budget is used up; the run can be resumed later."""


class _RateBudget:
    """Per-connection request budgets: a sliding 60-second window (rpm_limit) and a
    UTC-day counter (rpd_limit). A limit of 0 means no limit.

    The lock is held only to read and update counters, never while waiting, so a
    throttled connection cannot stall calls to other connections. After a wait the
    budget is checked again, so concurrent callers cannot burst past the limit.
    Counters live in this process; with several replicas each enforces its own share.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._minute: Dict[str, "deque[float]"] = {}
        self._day: Dict[str, tuple] = {}

    def _try_take(self, cid: str, rpm: int, rpd: int, now: float) -> float:
        """Take a slot and return 0.0, or return the seconds to wait. Raises when the day is used up."""
        with self._lock:
            day = time.strftime("%Y-%m-%d", time.gmtime(now))
            d, used = self._day.get(cid, (day, 0))
            if d != day:
                used = 0
            if rpd > 0 and used >= rpd:
                raise RateBudgetExhausted(
                    f"Daily request budget for connection {cid} is used up ({rpd} requests); "
                    "resume the run after 00:00 UTC."
                )
            if rpm > 0:
                q = self._minute.setdefault(cid, deque())
                while q and q[0] <= now - 60.0:
                    q.popleft()
                if len(q) >= rpm:
                    return max(0.01, q[0] + 60.0 - now)
                q.append(now)
            self._day[cid] = (day, used + 1)
            return 0.0

    def acquire(self, cid: str, rpm: int, rpd: int) -> None:
        if rpm <= 0 and rpd <= 0:
            return
        while True:
            wait = self._try_take(cid, rpm, rpd, time.time())
            if wait <= 0:
                return
            time.sleep(wait)

    async def aacquire(self, cid: str, rpm: int, rpd: int) -> None:
        if rpm <= 0 and rpd <= 0:
            return
        while True:
            wait = self._try_take(cid, rpm, rpd, time.time())
            if wait <= 0:
                return
            await asyncio.sleep(wait)


class ModelGateway:
    """The central outbound gateway through which every model call must pass."""

    def __init__(self, db=None, transport: Optional[httpx.BaseTransport] = None):
        self._db = db
        self._transport = transport
        self._async_semaphores: Dict[str, asyncio.Semaphore] = {}
        self._sync_semaphores: Dict[str, threading.Semaphore] = {}
        self._lock = threading.Lock()
        self._dev_cache: Dict[str, GatewayResponse] = {}
        self._budget = _RateBudget()

    @property
    def db(self):
        if self._db is not None:
            return self._db
        try:
            from backend.deps import db
            return db
        except Exception:
            return None

    def set_transport(self, transport: Optional[httpx.BaseTransport]) -> None:
        """Override HTTP transport (e.g. for mock testing)."""
        self._transport = transport

    def _resolve_connection(self, connector_id: Optional[str]) -> Dict[str, Any]:
        """Resolve connector configuration and credentials from DB or environment."""
        conn_info: Dict[str, Any] = {
            "connector_id": connector_id or "default",
            "base_url": "",
            "api_key": "",
            "provider_type": "openai",
            "deployment_kind": "openai_compatible",
            "model_family": "other",
            "default_model": "gpt-4o-mini",
            "azure_deployment": "",
            "api_version": "2024-08-01-preview",
            "azure_deployment_type": "standard",
            "inference_region": os.getenv("FM_DEFAULT_REGION", "me-central-1"),
            "max_concurrency": int(os.getenv("FM_CONNECTOR_MAX_CONCURRENCY", "4")),
            "rpm_limit": 0,
            "rpd_limit": 0,
            "max_input_tokens": 0,
            "timeout_s": 60.0,
        }

        if self.db is not None:
            if connector_id:
                creds = self.db.get_provider_credentials(connector_id)
                if creds:
                    conn_info.update(creds)
                    conn_info["connector_id"] = connector_id
            else:
                default_creds = self.db.get_default_provider_credentials()
                if default_creds:
                    conn_info.update(default_creds)
                    conn_info["connector_id"] = default_creds.get("provider_id", "default")

        # Fallback to legacy environment variables if needed
        if not conn_info.get("api_key") and not conn_info.get("base_url"):
            env_key = os.getenv("FM_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
            env_base = os.getenv("FM_JUDGE_BASE_URL", "https://api.openai.com/v1")
            env_model = os.getenv("FM_JUDGE_MODEL", "gpt-4o-mini")
            if env_key or env_base:
                conn_info["api_key"] = env_key or ""
                conn_info["base_url"] = env_base
                conn_info["default_model"] = env_model

        # Normalize deployment_kind from provider_type if not explicitly set
        ptype = conn_info.get("provider_type", "openai")
        if ptype == "azure_openai":
            conn_info["deployment_kind"] = "azure_openai"
        elif "deployment_kind" not in conn_info or not conn_info["deployment_kind"]:
            conn_info["deployment_kind"] = "openai_compatible"

        return conn_info

    def _get_target_host(self, conn_info: Dict[str, Any]) -> str:
        base_url = conn_info.get("base_url", "")
        if not base_url:
            base_url = "https://api.openai.com"
        target = base_url if "://" in base_url else f"http://{base_url}"
        try:
            return urlparse(target).hostname or ""
        except Exception:
            return ""

    def _check_and_log_egress(
        self,
        conn_info: Dict[str, Any],
        target_model: str,
        purpose: str,
        eval_run_id: Optional[str],
        actor: str,
    ) -> tuple[str, bool, bool]:
        """Validate egress. Raises EgressBlocked before any socket opens if prohibited.

        Returns (host, is_internal, cross_border).
        """
        host = self._get_target_host(conn_info)
        is_internal = is_internal_host(host)
        allow_external = os.getenv("FM_ALLOW_EXTERNAL_LLM_EGRESS", "false").lower() in ("true", "1", "yes")

        if not is_internal and not allow_external:
            # Audit the blocked egress attempt
            if self.db is not None:
                try:
                    self.db.log_gateway_call(
                        purpose=purpose,
                        host=host,
                        decision="blocked",
                        connector_id=conn_info.get("connector_id", ""),
                        is_internal=False,
                        model=target_model,
                        http_status=403,
                        latency_ms=0,
                        tokens_in=0,
                        tokens_out=0,
                        cross_border=False,
                        eval_run_id=eval_run_id,
                        actor=actor,
                    )
                except Exception as log_err:
                    logger.warning("Failed to write blocked gateway call log: %s", log_err)

            raise EgressBlocked("External model egress is disabled (FM_ALLOW_EXTERNAL_LLM_EGRESS)")

        # Cross border evaluation
        default_region = os.getenv("FM_DEFAULT_REGION", "me-central-1")
        conn_region = conn_info.get("inference_region") or default_region
        cross_border = False
        if not is_internal:
            is_azure_global = conn_info.get("azure_deployment_type") in ("global_standard", "global_provisioned")
            if conn_region != default_region or is_azure_global:
                cross_border = True

        return host, is_internal, cross_border

    def _get_sync_semaphore(self, connector_id: str, max_concurrency: int) -> threading.Semaphore:
        with self._lock:
            if connector_id not in self._sync_semaphores:
                self._sync_semaphores[connector_id] = threading.Semaphore(max_concurrency)
            return self._sync_semaphores[connector_id]

    def _get_async_semaphore(self, connector_id: str, max_concurrency: int) -> asyncio.Semaphore:
        with self._lock:
            if connector_id not in self._async_semaphores:
                self._async_semaphores[connector_id] = asyncio.Semaphore(max_concurrency)
            return self._async_semaphores[connector_id]

    def _make_adapter(self, conn_info: Dict[str, Any]):
        deployment_kind = conn_info.get("deployment_kind", "openai_compatible")
        if deployment_kind == "azure_openai":
            return AzureOpenAIConnector(
                endpoint=conn_info.get("base_url", ""),
                api_key=conn_info.get("api_key", ""),
                azure_deployment=conn_info.get("azure_deployment") or conn_info.get("default_model", ""),
                api_version=conn_info.get("api_version", "2024-08-01-preview"),
                azure_deployment_type=conn_info.get("azure_deployment_type", "standard"),
                inference_region=conn_info.get("inference_region", "me-central-1"),
                timeout_s=float(conn_info.get("timeout_s", 60.0)),
            )
        return OpenAICompatibleConnector(
            base_url=conn_info.get("base_url", ""),
            api_key=conn_info.get("api_key", ""),
            default_model=conn_info.get("default_model", "gpt-4o-mini"),
            timeout_s=float(conn_info.get("timeout_s", 60.0)),
        )

    def _get_dev_cache_key(
        self,
        connector_id: str,
        model: str,
        temperature: float,
        max_tokens: int,
        messages: List[Dict[str, Any]],
    ) -> str:
        payload = f"{connector_id}:{model}:{temperature}:{max_tokens}:{json.dumps(messages, sort_keys=True)}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _validate_input_tokens(self, messages: List[Dict[str, Any]], max_input_tokens: int) -> None:
        if max_input_tokens <= 0:
            return
        approx_tokens = sum(len(str(m.get("content", "")).split()) * 4 // 3 for m in messages)
        if approx_tokens > max_input_tokens:
            raise ValueError(
                f"Input tokens (~{approx_tokens}) exceeds connection max_input_tokens limit ({max_input_tokens})"
            )

    def chat(
        self,
        connector_id: Optional[str],
        messages: List[Dict[str, Any]],
        *,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        purpose: str,
        eval_run_id: Optional[str] = None,
        actor: str = "system",
    ) -> GatewayResponse:
        """Synchronously execute chat completion through the gateway."""
        conn_info = self._resolve_connection(connector_id)
        target_model = model or conn_info.get("default_model", "gpt-4o-mini")

        self._validate_input_tokens(messages, int(conn_info.get("max_input_tokens") or 0))

        host, is_internal, cross_border = self._check_and_log_egress(
            conn_info, target_model, purpose, eval_run_id, actor
        )

        use_dev_cache = (
            os.getenv("FM_DEV_RESPONSE_CACHE", "false").lower() in ("true", "1", "yes")
            and purpose != "canary"
        )
        if use_dev_cache:
            ckey = self._get_dev_cache_key(
                conn_info["connector_id"], target_model, temperature, max_tokens, messages
            )
            with self._lock:
                if ckey in self._dev_cache:
                    return self._dev_cache[ckey]

        sem = self._get_sync_semaphore(
            conn_info["connector_id"], conn_info.get("max_concurrency", 4)
        )
        adapter = self._make_adapter(conn_info)

        attempts = 3
        last_exc: Optional[Exception] = None
        t_start = time.time()

        with sem:
            self._budget.acquire(conn_info["connector_id"], int(conn_info.get("rpm_limit") or 0), int(conn_info.get("rpd_limit") or 0))
            for attempt in range(1, attempts + 1):
                try:
                    raw = adapter.chat(
                        messages,
                        model=target_model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        timeout=float(conn_info.get("timeout_s") or 60.0),
                        transport=self._transport,
                    )
                    resp = GatewayResponse(
                        text=raw["text"],
                        model_reported=raw["model_reported"],
                        system_fingerprint=raw.get("system_fingerprint", ""),
                        finish_reason=raw.get("finish_reason", "stop"),
                        tokens_in=raw.get("tokens_in", 0),
                        tokens_out=raw.get("tokens_out", 0),
                        latency_ms=raw.get("latency_ms", int((time.time() - t_start) * 1000)),
                        connector_id=conn_info["connector_id"],
                        inference_region=conn_info.get("inference_region", os.getenv("FM_DEFAULT_REGION", "me-central-1")),
                        cross_border=cross_border,
                    )

                    if self.db is not None:
                        try:
                            self.db.log_gateway_call(
                                purpose=purpose,
                                host=host,
                                decision="allowed",
                                connector_id=conn_info["connector_id"],
                                is_internal=is_internal,
                                model=resp.model_reported,
                                http_status=200,
                                latency_ms=resp.latency_ms,
                                tokens_in=resp.tokens_in,
                                tokens_out=resp.tokens_out,
                                cross_border=cross_border,
                                eval_run_id=eval_run_id,
                                actor=actor,
                            )
                        except Exception as log_err:
                            logger.warning("Failed to write gateway call log: %s", log_err)

                    if use_dev_cache:
                        with self._lock:
                            self._dev_cache[ckey] = resp

                    return resp

                except httpx.HTTPStatusError as e:
                    last_exc = e
                    status = e.response.status_code
                    if status == 429 or status >= 500:
                        retry_after = e.response.headers.get("Retry-After")
                        sleep_s = float(retry_after) if retry_after and retry_after.isdigit() else (0.5 * (2 ** (attempt - 1)))
                        if attempt < attempts:
                            time.sleep(min(sleep_s, 10.0))
                            continue
                    break

                except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RequestError) as e:
                    last_exc = e
                    if attempt < attempts:
                        time.sleep(0.5 * (2 ** (attempt - 1)))
                        continue
                    break

                except Exception as e:
                    last_exc = e
                    break

        # Log error on failure
        total_latency = int((time.time() - t_start) * 1000)
        status_code = getattr(getattr(last_exc, "response", None), "status_code", 500)
        if self.db is not None:
            try:
                self.db.log_gateway_call(
                    purpose=purpose,
                    host=host,
                    decision="error",
                    connector_id=conn_info["connector_id"],
                    is_internal=is_internal,
                    model=target_model,
                    http_status=status_code,
                    latency_ms=total_latency,
                    tokens_in=0,
                    tokens_out=0,
                    cross_border=cross_border,
                    eval_run_id=eval_run_id,
                    actor=actor,
                )
            except Exception as log_err:
                logger.warning("Failed to write gateway error log: %s", log_err)

        raise last_exc or RuntimeError("Unknown gateway failure")

    async def achat(
        self,
        connector_id: Optional[str],
        messages: List[Dict[str, Any]],
        *,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        purpose: str,
        eval_run_id: Optional[str] = None,
        actor: str = "system",
    ) -> GatewayResponse:
        """Asynchronously execute chat completion through the gateway."""
        conn_info = self._resolve_connection(connector_id)
        target_model = model or conn_info.get("default_model", "gpt-4o-mini")

        self._validate_input_tokens(messages, int(conn_info.get("max_input_tokens") or 0))

        host, is_internal, cross_border = self._check_and_log_egress(
            conn_info, target_model, purpose, eval_run_id, actor
        )

        use_dev_cache = (
            os.getenv("FM_DEV_RESPONSE_CACHE", "false").lower() in ("true", "1", "yes")
            and purpose != "canary"
        )
        if use_dev_cache:
            ckey = self._get_dev_cache_key(
                conn_info["connector_id"], target_model, temperature, max_tokens, messages
            )
            with self._lock:
                if ckey in self._dev_cache:
                    return self._dev_cache[ckey]

        sem = self._get_async_semaphore(
            conn_info["connector_id"], conn_info.get("max_concurrency", 4)
        )
        adapter = self._make_adapter(conn_info)

        attempts = 3
        last_exc: Optional[Exception] = None
        t_start = time.time()

        async with sem:
            await self._budget.aacquire(conn_info["connector_id"], int(conn_info.get("rpm_limit") or 0), int(conn_info.get("rpd_limit") or 0))
            for attempt in range(1, attempts + 1):
                try:
                    raw = await adapter.achat(
                        messages,
                        model=target_model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        timeout=float(conn_info.get("timeout_s") or 60.0),
                        transport=self._transport,
                    )
                    resp = GatewayResponse(
                        text=raw["text"],
                        model_reported=raw["model_reported"],
                        system_fingerprint=raw.get("system_fingerprint", ""),
                        finish_reason=raw.get("finish_reason", "stop"),
                        tokens_in=raw.get("tokens_in", 0),
                        tokens_out=raw.get("tokens_out", 0),
                        latency_ms=raw.get("latency_ms", int((time.time() - t_start) * 1000)),
                        connector_id=conn_info["connector_id"],
                        inference_region=conn_info.get("inference_region", os.getenv("FM_DEFAULT_REGION", "me-central-1")),
                        cross_border=cross_border,
                    )

                    if self.db is not None:
                        try:
                            self.db.log_gateway_call(
                                purpose=purpose,
                                host=host,
                                decision="allowed",
                                connector_id=conn_info["connector_id"],
                                is_internal=is_internal,
                                model=resp.model_reported,
                                http_status=200,
                                latency_ms=resp.latency_ms,
                                tokens_in=resp.tokens_in,
                                tokens_out=resp.tokens_out,
                                cross_border=cross_border,
                                eval_run_id=eval_run_id,
                                actor=actor,
                            )
                        except Exception as log_err:
                            logger.warning("Failed to write gateway call log: %s", log_err)

                    if use_dev_cache:
                        with self._lock:
                            self._dev_cache[ckey] = resp

                    return resp

                except httpx.HTTPStatusError as e:
                    last_exc = e
                    status = e.response.status_code
                    if status == 429 or status >= 500:
                        retry_after = e.response.headers.get("Retry-After")
                        sleep_s = float(retry_after) if retry_after and retry_after.isdigit() else (0.5 * (2 ** (attempt - 1)))
                        if attempt < attempts:
                            await asyncio.sleep(min(sleep_s, 10.0))
                            continue
                    break

                except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RequestError) as e:
                    last_exc = e
                    if attempt < attempts:
                        await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
                        continue
                    break

                except Exception as e:
                    last_exc = e
                    break

        # Log error on failure
        total_latency = int((time.time() - t_start) * 1000)
        status_code = getattr(getattr(last_exc, "response", None), "status_code", 500)
        if self.db is not None:
            try:
                self.db.log_gateway_call(
                    purpose=purpose,
                    host=host,
                    decision="error",
                    connector_id=conn_info["connector_id"],
                    is_internal=is_internal,
                    model=target_model,
                    http_status=status_code,
                    latency_ms=total_latency,
                    tokens_in=0,
                    tokens_out=0,
                    cross_border=cross_border,
                    eval_run_id=eval_run_id,
                    actor=actor,
                )
            except Exception as log_err:
                logger.warning("Failed to write gateway error log: %s", log_err)

        raise last_exc or RuntimeError("Unknown gateway failure")

    def embed(
        self,
        connector_id: Optional[str],
        texts: List[str],
        *,
        purpose: str,
        model: Optional[str] = None,
        eval_run_id: Optional[str] = None,
        actor: str = "system",
    ) -> List[List[float]]:
        """Synchronously generate text embeddings through the gateway."""
        conn_info = self._resolve_connection(connector_id)
        target_model = model or "text-embedding-3-small"

        host, is_internal, cross_border = self._check_and_log_egress(
            conn_info, target_model, purpose, eval_run_id, actor
        )

        adapter = self._make_adapter(conn_info)
        t_start = time.time()
        try:
            embeddings = adapter.embed(
                texts,
                model=target_model,
                timeout=30.0,
                transport=self._transport,
            )
            latency_ms = int((time.time() - t_start) * 1000)

            if self.db is not None:
                try:
                    self.db.log_gateway_call(
                        purpose=purpose,
                        host=host,
                        decision="allowed",
                        connector_id=conn_info["connector_id"],
                        is_internal=is_internal,
                        model=target_model,
                        http_status=200,
                        latency_ms=latency_ms,
                        tokens_in=len(texts) * 5,
                        tokens_out=0,
                        cross_border=cross_border,
                        eval_run_id=eval_run_id,
                        actor=actor,
                    )
                except Exception as log_err:
                    logger.warning("Failed to write gateway embed log: %s", log_err)

            return embeddings

        except Exception as e:
            latency_ms = int((time.time() - t_start) * 1000)
            status_code = getattr(getattr(e, "response", None), "status_code", 500)
            if self.db is not None:
                try:
                    self.db.log_gateway_call(
                        purpose=purpose,
                        host=host,
                        decision="error",
                        connector_id=conn_info["connector_id"],
                        is_internal=is_internal,
                        model=target_model,
                        http_status=status_code,
                        latency_ms=latency_ms,
                        tokens_in=0,
                        tokens_out=0,
                        cross_border=cross_border,
                        eval_run_id=eval_run_id,
                        actor=actor,
                    )
                except Exception as log_err:
                    logger.warning("Failed to write gateway embed error log: %s", log_err)
            raise


# Global gateway instance
gateway = ModelGateway()
