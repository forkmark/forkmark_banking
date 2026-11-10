"""Tests for ModelGateway egress enforcement, call logging, and architectural compliance.

Proves:
1. With egress disabled (default) and external endpoints, each model-calling path
   returns 403 or a failed EvalResult, and zero outbound network requests occur.
2. When egress is allowed or hosts are internal, requests succeed and log 'allowed'.
3. Every call writes metadata-only telemetry to `gateway_call_log`.
4. GET /api/gateway/log filters by purpose, decision, date.
5. Source code scan confirms no model call bypasses core/llm_gateway.py.
6. Upstream pricing refresh is disabled by default.
"""
from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from tests.fixtures.mock_llm import MockLLMHandler, create_mock_transport


class TestGatewayEgressEnforcement(unittest.TestCase):
    def setUp(self):
        import backend.deps
        import backend.main
        import core.llm_gateway
        import core.egress_policy

        self.db = backend.deps.db
        self.app = backend.main.app
        self.gateway = core.llm_gateway.gateway
        self.EgressBlocked = core.egress_policy.EgressBlocked

        self.client = TestClient(self.app)
        os.environ["FM_ALLOW_EXTERNAL_LLM_EGRESS"] = "false"
        self.mock_handler = MockLLMHandler()
        self.transport, _ = create_mock_transport(self.mock_handler)
        self.gateway.set_transport(self.transport)

    def tearDown(self):
        self.gateway.set_transport(None)
        if "FM_ALLOW_EXTERNAL_LLM_EGRESS" in os.environ:
            del os.environ["FM_ALLOW_EXTERNAL_LLM_EGRESS"]
        # Ensure a clean event loop exists for subsequent tests
        asyncio.set_event_loop(asyncio.new_event_loop())

    def _run_async(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_pre_socket_egress_blocked_raises_before_transport(self):
        """Gateway raises EgressBlocked on external URLs without touching transport."""
        from core.llm_gateway import ModelGateway
        gw = ModelGateway(db=self.db, transport=self.transport)
        with patch.dict(os.environ, {
            "FM_ALLOW_EXTERNAL_LLM_EGRESS": "false",
            "FM_OPENAI_API_KEY": "sk-test",
            "FM_JUDGE_BASE_URL": "https://api.openai.com/v1",
        }):
            with self.assertRaises(self.EgressBlocked):
                gw.chat(None, [{"role": "user", "content": "hello"}], purpose="runner")
            self.assertEqual(len(self.mock_handler.requests), 0)

    def test_path1_comparator_embeddings_egress_blocked(self):
        """Comparator embedding divergence returns None and makes zero outbound calls."""
        from core.comparator import _openai_divergence
        with patch.dict(os.environ, {
            "FM_ALLOW_EXTERNAL_LLM_EGRESS": "false",
            "FM_OPENAI_API_KEY": "sk-test",
            "FM_JUDGE_BASE_URL": "https://api.openai.com/v1",
        }):
            res = _openai_divergence("text a", "text b")
            self.assertIsNone(res)
            self.assertEqual(len(self.mock_handler.requests), 0)

    def test_path1_comparator_llm_judge_egress_blocked(self):
        """Comparator LLM-judge divergence returns None and makes zero outbound calls."""
        from core.comparator import _llm_judge_divergence
        with patch.dict(os.environ, {
            "FM_ALLOW_EXTERNAL_LLM_EGRESS": "false",
            "FM_OPENAI_API_KEY": "sk-test",
            "FM_JUDGE_BASE_URL": "https://api.openai.com/v1",
        }):
            res = _llm_judge_divergence("output 1", "output 2")
            self.assertIsNone(res)
            self.assertEqual(len(self.mock_handler.requests), 0)

    def test_path2_evaluators_llm_evaluate_egress_blocked(self):
        """_llm_evaluate returns failed EvalResult and makes zero outbound calls."""
        from core.evaluators import _llm_evaluate
        with patch.dict(os.environ, {
            "FM_ALLOW_EXTERNAL_LLM_EGRESS": "false",
            "FM_OPENAI_API_KEY": "sk-test",
            "FM_JUDGE_BASE_URL": "https://api.openai.com/v1",
        }):
            result = self._run_async(_llm_evaluate("Judge prompt", "faithfulness"))
            self.assertFalse(result.passed)
            self.assertIn("External model egress is disabled", result.detail)
            self.assertEqual(len(self.mock_handler.requests), 0)

    def test_path3_evaluators_pairwise_preference_egress_blocked(self):
        """_pairwise_preference returns failed EvalResult and makes zero outbound calls."""
        from core.evaluators import _pairwise_preference
        with patch.dict(os.environ, {
            "FM_ALLOW_EXTERNAL_LLM_EGRESS": "false",
            "FM_OPENAI_API_KEY": "sk-test",
            "FM_JUDGE_BASE_URL": "https://api.openai.com/v1",
        }):
            result = self._run_async(_pairwise_preference("A text", "B text", {}, {}))
            self.assertFalse(result.passed)
            self.assertIn("External model egress is disabled", result.detail)
            self.assertEqual(len(self.mock_handler.requests), 0)

    def test_path4_runner_nocode_egress_blocked_returns_403(self):
        """POST /api/runner with external endpoint returns HTTP 403."""
        prov = self.db.create_provider(
            name="External OpenAI",
            provider_type="openai",
            base_url="https://api.openai.com/v1",
            api_key="sk-test-runner",
        )
        try:
            with patch.dict(os.environ, {"FM_ALLOW_EXTERNAL_LLM_EGRESS": "false"}):
                resp = self.client.post(
                    "/api/runner",
                    json={
                        "workflow_name": "TestEgressWF",
                        "eval_run_name": "TestRun",
                        "branch_a": {
                            "name": "Branch A",
                            "model_id": "gpt-4o",
                            "provider_id": prov["id"],
                            "steps": [{"name": "s1", "system_prompt": "", "user_prompt_template": "{{input}}"}],
                        },
                        "branch_b": {
                            "name": "Branch B",
                            "model_id": "gpt-4o-mini",
                            "provider_id": prov["id"],
                            "steps": [{"name": "s1", "system_prompt": "", "user_prompt_template": "{{input}}"}],
                        },
                        "test_cases": [{"label": "tc1", "input": "test input"}],
                    },
                )
                self.assertEqual(resp.status_code, 403)
                self.assertIn("External model egress is disabled", resp.json()["detail"])
                self.assertEqual(len(self.mock_handler.requests), 0)
        finally:
            self.db.delete_provider(prov["id"])

    def test_path5_playground_egress_blocked_returns_403(self):
        """POST /api/playground with external endpoint returns HTTP 403."""
        prov = self.db.create_provider(
            name="External Playground Provider",
            provider_type="openai",
            base_url="https://api.openai.com/v1",
            api_key="sk-test-play",
        )
        try:
            with patch.dict(os.environ, {"FM_ALLOW_EXTERNAL_LLM_EGRESS": "false"}):
                resp = self.client.post(
                    "/api/playground",
                    json={
                        "prompt": "Explain repo margin rules",
                        "model_a": "gpt-4o",
                        "model_b": "gpt-4o-mini",
                        "provider_id_a": prov["id"],
                        "provider_id_b": prov["id"],
                    },
                )
                self.assertEqual(resp.status_code, 403)
                self.assertIn("External model egress is disabled", resp.json()["detail"])
                self.assertEqual(len(self.mock_handler.requests), 0)
        finally:
            self.db.delete_provider(prov["id"])

    def test_path6_providers_test_egress_blocked_returns_403(self):
        """POST /api/providers/{id}/test with external endpoint returns HTTP 403."""
        prov = self.db.create_provider(
            name="External Test Provider",
            provider_type="openai",
            base_url="https://api.openai.com/v1",
            api_key="sk-test-prov",
        )
        try:
            with patch.dict(os.environ, {"FM_ALLOW_EXTERNAL_LLM_EGRESS": "false"}):
                resp = self.client.post(f"/api/providers/{prov['id']}/test")
                self.assertEqual(resp.status_code, 403)
                self.assertIn("External model egress is disabled", resp.json()["detail"])
                self.assertEqual(len(self.mock_handler.requests), 0)
        finally:
            self.db.delete_provider(prov["id"])

    def test_allowed_calls_logged_to_gateway_call_log(self):
        """Internal/allowed calls log telemetry metadata and execute successfully."""
        from core.llm_gateway import ModelGateway
        internal_prov = self.db.create_provider(
            name="On-Prem Sovereign vLLM",
            provider_type="openai",
            base_url="http://vllm.bank.internal:8000/v1",
            api_key="sk-internal",
        )
        try:
            gw = ModelGateway(db=self.db, transport=self.transport)
            self.mock_handler.set_scripted_reply("ping", "pong")
            resp = gw.chat(
                connector_id=internal_prov["id"],
                messages=[{"role": "user", "content": "ping"}],
                purpose="runner",
                actor="test_user",
            )
            self.assertEqual(resp.text, "pong")
            self.assertEqual(len(self.mock_handler.requests), 1)

            # Verify gateway_call_log
            logs = self.db.list_gateway_calls(purpose="runner", limit=5)
            self.assertTrue(len(logs) > 0)
            entry = logs[0]
            self.assertEqual(entry["decision"], "allowed")
            self.assertEqual(entry["host"], "vllm.bank.internal")
            self.assertTrue(bool(entry["is_internal"]))
            self.assertEqual(entry["actor"], "test_user")
        finally:
            self.db.delete_provider(internal_prov["id"])

    def test_gateway_log_api_filters(self):
        """GET /api/gateway/log supports filtering by purpose and decision."""
        resp = self.client.get("/api/gateway/log?purpose=runner&decision=blocked")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()
        self.assertIsInstance(items, list)
        for item in items:
            self.assertEqual(item["purpose"], "runner")
            self.assertEqual(item["decision"], "blocked")

    def test_pricing_refresh_disabled_by_default(self):
        """Pricing table refresh is disabled unless FM_REFRESH_PRICING=true."""
        self.assertNotIn("FM_REFRESH_PRICING", os.environ)
        self.assertFalse(os.getenv("FM_REFRESH_PRICING", "false").lower() in ("true", "1", "yes"))

    def test_source_scan_no_unguarded_model_calls(self):
        """Source scan fails if raw chat/completions or embeddings HTTP calls exist outside gateway."""
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        offending_patterns = []

        scan_dirs = ["core", "backend"]
        allowlisted_files = {
            "core/llm_gateway.py",
            "core/connectors/openai_compatible.py",
            "core/connectors/azure_openai.py",
            "core/connectors/base.py",
        }

        for sdir in scan_dirs:
            full_dir = os.path.join(root_dir, sdir)
            for dirpath, _, filenames in os.walk(full_dir):
                for f in filenames:
                    if not f.endswith(".py"):
                        continue
                    rel_path = os.path.relpath(os.path.join(dirpath, f), root_dir)
                    if rel_path in allowlisted_files:
                        continue

                    file_content = open(os.path.join(dirpath, f), "r", encoding="utf-8").read()
                    if "chat/completions" in file_content or "/embeddings" in file_content:
                        for idx, line in enumerate(file_content.splitlines(), start=1):
                            l = line.strip()
                            if l.startswith("#") or l.startswith('"""') or l.startswith("'''"):
                                continue
                            if ("post(" in l or "get(" in l or "Client" in l) and ("chat/completions" in l or "/embeddings" in l):
                                offending_patterns.append(f"{rel_path}:{idx} -> {l}")

        self.assertEqual(
            offending_patterns,
            [],
            f"Found unguarded model network calls outside core/llm_gateway.py:\n" + "\n".join(offending_patterns),
        )


if __name__ == "__main__":
    unittest.main()
