"""Shared mock LLM endpoint built on httpx.MockTransport.

Supports:
- Scripted replies keyed by prompt substring
- Switchable `model` and `system_fingerprint` fields to simulate vendor model swaps
- 429 / 5xx error injection and transient retry testing
- Slow reply simulation (latency injection)
- OpenAI-compatible and Azure OpenAI endpoint shapes
- Embeddings requests
"""
from __future__ import annotations

import json
import time
from typing import Dict, List, Optional
import httpx


class MockLLMHandler:
    def __init__(
        self,
        default_model: str = "mock-model-v1",
        default_reply: str = "Mock response from sovereign model.",
        system_fingerprint: str = "fp_mock_001",
    ):
        self.default_model = default_model
        self.reported_model = default_model
        self.default_reply = default_reply
        self.system_fingerprint = system_fingerprint
        self.replies_by_keyword: Dict[str, str] = {}
        self.fail_status: Optional[int] = None
        self.fail_countdown: int = 0
        self.retry_after: Optional[str] = None
        self.delay_s: float = 0.0
        self.requests: List[httpx.Request] = []

    def set_scripted_reply(self, keyword: str, reply: str) -> None:
        self.replies_by_keyword[keyword] = reply

    def inject_transient_error(self, status_code: int, times: int = 1, retry_after: Optional[str] = None) -> None:
        self.fail_status = status_code
        self.fail_countdown = times
        self.retry_after = retry_after

    def set_reported_model(self, model: str, fingerprint: Optional[str] = None) -> None:
        self.reported_model = model
        if fingerprint is not None:
            self.system_fingerprint = fingerprint

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)

        if self.delay_s > 0:
            time.sleep(self.delay_s)

        if self.fail_countdown > 0:
            self.fail_countdown -= 1
            headers = {}
            if self.retry_after:
                headers["Retry-After"] = self.retry_after
            return httpx.Response(
                status_code=self.fail_status or 500,
                headers=headers,
                json={"error": {"message": f"Injected error {self.fail_status}"}},
                request=request,
            )

        url_path = request.url.path

        # Embeddings endpoint
        if url_path.endswith("/embeddings"):
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
            inputs = body.get("input", [""])
            if isinstance(inputs, str):
                inputs = [inputs]
            data = [
                {
                    "object": "embedding",
                    "index": i,
                    "embedding": [0.01 * (i + 1), 0.02 * (i + 1), 0.03 * (i + 1)],
                }
                for i in range(len(inputs))
            ]
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": data,
                    "model": self.reported_model,
                    "usage": {"prompt_tokens": len(inputs) * 5, "total_tokens": len(inputs) * 5},
                },
                request=request,
            )

        # Models list endpoint
        if url_path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [{"id": self.reported_model, "object": "model"}],
                },
                request=request,
            )

        # Chat completions endpoint (OpenAI or Azure)
        if "chat/completions" in url_path or url_path.endswith("/messages"):
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
            messages = body.get("messages", [])
            last_prompt = messages[-1].get("content", "") if messages else ""

            reply_text = self.default_reply
            for kw, rep in self.replies_by_keyword.items():
                if kw in last_prompt:
                    reply_text = rep
                    break

            tokens_in = max(1, len(last_prompt.split()))
            tokens_out = max(1, len(reply_text.split()))

            # If Anthropic /messages format
            if url_path.endswith("/messages"):
                resp_json = {
                    "id": "msg_mock_001",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": reply_text}],
                    "model": self.reported_model,
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
                }
            else:
                resp_json = {
                    "id": "chatcmpl_mock_001",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": self.reported_model,
                    "system_fingerprint": self.system_fingerprint,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": reply_text},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": tokens_in,
                        "completion_tokens": tokens_out,
                        "total_tokens": tokens_in + tokens_out,
                    },
                }

            return httpx.Response(200, json=resp_json, request=request)

        return httpx.Response(404, json={"error": "Not found in mock"}, request=request)


def create_mock_transport(handler: Optional[MockLLMHandler] = None) -> tuple[httpx.MockTransport, MockLLMHandler]:
    h = handler or MockLLMHandler()
    return httpx.MockTransport(h.handle_request), h

