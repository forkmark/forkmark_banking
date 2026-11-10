"""OpenAI-compatible model connector adapter (vLLM, TGI, SGLang, Ollama, OpenAI, OpenRouter)."""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional
import httpx


class OpenAICompatibleConnector:
    """Adapter for endpoints implementing the OpenAI chat/completions wire format."""

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        default_model: str = "gpt-4o-mini",
        stop_tokens: Optional[List[str]] = None,
        timeout_s: float = 60.0,
    ):
        raw_base = (base_url or "https://api.openai.com").rstrip("/")
        if raw_base.endswith("/v1"):
            self.base_v1 = raw_base
        else:
            self.base_v1 = f"{raw_base}/v1"
        self.api_key = api_key or ""
        self.default_model = default_model or "gpt-4o-mini"
        self.stop_tokens = stop_tokens or []
        self.timeout_s = timeout_s

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        timeout: Optional[float] = None,
        transport: Optional[Any] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_v1}/chat/completions"
        target_model = model or self.default_model
        payload: Dict[str, Any] = {
            "model": target_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self.stop_tokens:
            payload["stop"] = self.stop_tokens

        t0 = time.time()
        with httpx.Client(transport=transport, timeout=timeout or self.timeout_s) as client:
            resp = client.post(url, headers=self._headers(), json=payload)
            latency_ms = int((time.time() - t0) * 1000)
            resp.raise_for_status()
            data = resp.json()

        choice = data.get("choices", [{}])[0]
        usage = data.get("usage", {})
        return {
            "text": choice.get("message", {}).get("content", ""),
            "model_reported": data.get("model", target_model),
            "system_fingerprint": data.get("system_fingerprint", ""),
            "finish_reason": choice.get("finish_reason", "stop"),
            "tokens_in": usage.get("prompt_tokens", 0),
            "tokens_out": usage.get("completion_tokens", 0),
            "latency_ms": latency_ms,
            "http_status": resp.status_code,
        }

    async def achat(
        self,
        messages: List[Dict[str, Any]],
        *,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        timeout: Optional[float] = None,
        transport: Optional[Any] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_v1}/chat/completions"
        target_model = model or self.default_model
        payload: Dict[str, Any] = {
            "model": target_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self.stop_tokens:
            payload["stop"] = self.stop_tokens

        t0 = time.time()
        async with httpx.AsyncClient(transport=transport, timeout=timeout or self.timeout_s) as client:
            resp = await client.post(url, headers=self._headers(), json=payload)
            latency_ms = int((time.time() - t0) * 1000)
            resp.raise_for_status()
            data = resp.json()

        choice = data.get("choices", [{}])[0]
        usage = data.get("usage", {})
        return {
            "text": choice.get("message", {}).get("content", ""),
            "model_reported": data.get("model", target_model),
            "system_fingerprint": data.get("system_fingerprint", ""),
            "finish_reason": choice.get("finish_reason", "stop"),
            "tokens_in": usage.get("prompt_tokens", 0),
            "tokens_out": usage.get("completion_tokens", 0),
            "latency_ms": latency_ms,
            "http_status": resp.status_code,
        }

    def embed(
        self,
        texts: List[str],
        *,
        model: Optional[str] = None,
        timeout: float = 30.0,
        transport: Optional[Any] = None,
    ) -> List[List[float]]:
        url = f"{self.base_v1}/embeddings"
        target_model = model or "text-embedding-3-small"
        payload = {"input": texts, "model": target_model}

        with httpx.Client(transport=transport, timeout=timeout) as client:
            resp = client.post(url, headers=self._headers(), json=payload)
            resp.raise_for_status()
            data = resp.json()

        embeddings_data = data.get("data", [])
        return [item.get("embedding", []) for item in embeddings_data]

    def health(
        self,
        *,
        timeout: float = 15.0,
        transport: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Verify connectivity by probing /models or a lightweight chat check."""
        t0 = time.time()
        with httpx.Client(transport=transport, timeout=timeout) as client:
            try:
                models_url = f"{self.base_v1}/models"
                resp = client.get(models_url, headers=self._headers())
                latency_ms = int((time.time() - t0) * 1000)
                if resp.status_code < 400:
                    data = resp.json()
                    models = [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict)]
                    first_model = models[0] if models else self.default_model
                    return {
                        "ok": True,
                        "latency_ms": latency_ms,
                        "model_reported": first_model,
                        "system_fingerprint": "",
                    }
            except Exception:
                pass

            # Fallback to chat check with max_tokens=1
            chat_res = self.chat(
                [{"role": "user", "content": "ping"}],
                max_tokens=1,
                timeout=timeout,
                transport=transport,
            )
            return {
                "ok": True,
                "latency_ms": chat_res["latency_ms"],
                "model_reported": chat_res["model_reported"],
                "system_fingerprint": chat_res["system_fingerprint"],
            }

