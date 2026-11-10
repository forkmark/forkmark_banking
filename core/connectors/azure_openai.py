"""Azure OpenAI model connector adapter."""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional
import httpx


class AzureOpenAIConnector:
    """Adapter for Microsoft Azure OpenAI Service endpoints."""

    def __init__(
        self,
        endpoint: str = "",
        api_key: str = "",
        azure_deployment: str = "",
        api_version: str = "2024-08-01-preview",
        azure_deployment_type: str = "standard",
        inference_region: str = "me-central-1",
        timeout_s: float = 60.0,
    ):
        self.endpoint = (endpoint or "").rstrip("/")
        self.api_key = api_key or ""
        self.azure_deployment = azure_deployment or ""
        self.api_version = api_version or "2024-08-01-preview"
        self.azure_deployment_type = (azure_deployment_type or "standard").lower()
        self.inference_region = inference_region or "me-central-1"
        self.timeout_s = timeout_s

    @property
    def is_global_deployment(self) -> bool:
        """True if Azure deployment type can route outside UAE on-soil boundary."""
        return self.azure_deployment_type in ("global_standard", "global_provisioned")

    def _headers(self) -> Dict[str, str]:
        return {
            "api-key": self.api_key,
            "Content-Type": "application/json",
        }

    def _chat_url(self, deployment: Optional[str] = None) -> str:
        dep = deployment or self.azure_deployment
        return f"{self.endpoint}/openai/deployments/{dep}/chat/completions?api-version={self.api_version}"

    def _embed_url(self, deployment: Optional[str] = None) -> str:
        dep = deployment or self.azure_deployment
        return f"{self.endpoint}/openai/deployments/{dep}/embeddings?api-version={self.api_version}"

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
        url = self._chat_url(model)
        payload = {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

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
            "model_reported": data.get("model", model or self.azure_deployment),
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
        url = self._chat_url(model)
        payload = {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

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
            "model_reported": data.get("model", model or self.azure_deployment),
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
        url = self._embed_url(model)
        payload = {"input": texts}

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

