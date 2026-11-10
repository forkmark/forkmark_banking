"""Tests for connectors (OpenAI-compatible, Azure OpenAI, presets) and Migration v17."""
import pytest
import httpx
from backend.deps import db
from core.connectors.openai_compatible import OpenAICompatibleConnector
from core.connectors.azure_openai import AzureOpenAIConnector
from core.connectors.presets import PRESETS, get_preset


def test_migration_v17_schema(tmp_path):
    """Verify Migration v17 columns and model_fingerprints table exist."""
    from core.store import Database
    db = Database(str(tmp_path / "v17_test.db"))
    # 1. llm_providers columns
    provider = db.create_provider(
        name="Test Inception Jais",
        provider_type="openai_compatible",
        base_url="https://api.inception.ai/v1",
        api_key="test-key-123",
        deployment_kind="openai_compatible",
        model_family="jais",
        default_model="inceptionai/Jais-2-8B-Chat",
        hosting="bank_self_hosted",
        inference_region="me-central-1",
        vendor_name="Inception AI",
        licence="Apache 2.0",
        max_concurrency=8,
        timeout_s=30.0,
    )
    assert provider["id"]
    fetched = db.get_provider(provider["id"])
    assert fetched["model_family"] == "jais"
    assert fetched["hosting"] == "bank_self_hosted"
    assert fetched["inference_region"] == "me-central-1"
    assert fetched["vendor_name"] == "Inception AI"
    assert fetched["max_concurrency"] == 8

    # 2. Update provider with Azure fields
    updated = db.update_provider(
        provider["id"],
        azure_deployment="gpt-4o-uaenorth",
        api_version="2024-06-01",
        azure_deployment_type="provisioned",
    )
    assert updated["azure_deployment"] == "gpt-4o-uaenorth"
    assert updated["api_version"] == "2024-06-01"
    assert updated["azure_deployment_type"] == "provisioned"

    # 3. model_fingerprints
    fp = db.record_fingerprint(
        governed_model_id="m-test-1",
        connector_id=provider["id"],
        model_reported="jais-2-8b-chat-v1",
        system_fingerprint="fp_jais_99",
        source="validation_run",
    )
    assert fp["id"]
    assert fp["model_reported"] == "jais-2-8b-chat-v1"

    fps = db.list_fingerprints(governed_model_id="m-test-1")
    assert len(fps) == 1
    assert fps[0]["system_fingerprint"] == "fp_jais_99"

    latest = db.get_latest_fingerprint("m-test-1")
    assert latest["model_reported"] == "jais-2-8b-chat-v1"


def test_openai_compatible_connector_url_normalization():
    """Accepts base_url with or without trailing /v1 without double /v1/v1."""
    captured_urls = []

    def mock_handler(request: httpx.Request):
        captured_urls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "choices": [{"message": {"role": "assistant", "content": "مرحبا"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "model": "jais-13b",
            },
        )

    transport = httpx.MockTransport(mock_handler)

    # 1. URL without /v1
    conn1 = OpenAICompatibleConnector(base_url="http://10.0.0.1:8000", api_key="test-k")
    res1 = conn1.chat([{"role": "user", "content": "hi"}], model="jais-13b", transport=transport)
    assert res1["text"] == "مرحبا"
    assert captured_urls[-1] == "http://10.0.0.1:8000/v1/chat/completions"

    # 2. URL with /v1
    conn2 = OpenAICompatibleConnector(base_url="http://10.0.0.1:8000/v1", api_key="test-k")
    res2 = conn2.chat([{"role": "user", "content": "hi"}], model="jais-13b", transport=transport)
    assert res2["text"] == "مرحبا"
    assert captured_urls[-1] == "http://10.0.0.1:8000/v1/chat/completions"

    # 3. URL with trailing slash /v1/
    conn3 = OpenAICompatibleConnector(base_url="http://10.0.0.1:8000/v1/", api_key="test-k")
    res3 = conn3.chat([{"role": "user", "content": "hi"}], model="jais-13b", transport=transport)
    assert res3["text"] == "مرحبا"
    assert captured_urls[-1] == "http://10.0.0.1:8000/v1/chat/completions"


def test_azure_openai_connector():
    """Formats deployment URL and passes api-key header."""
    captured_requests = []

    def mock_azure_handler(request: httpx.Request):
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-az-1",
                "choices": [{"message": {"role": "assistant", "content": "Azure response"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 15, "completion_tokens": 8},
                "model": "gpt-4o-2024-05-13",
                "system_fingerprint": "fp_az_44",
            },
        )

    transport = httpx.MockTransport(mock_azure_handler)

    conn = AzureOpenAIConnector(
        endpoint="https://bank-uaenorth.openai.azure.com",
        api_key="azure-secret-key",
        api_version="2024-06-01",
        azure_deployment="gpt-4o-dep",
    )

    resp = conn.chat([{"role": "user", "content": "Hello Azure"}], transport=transport)
    assert resp["text"] == "Azure response"
    assert resp["model_reported"] == "gpt-4o-2024-05-13"
    assert resp["system_fingerprint"] == "fp_az_44"

    req = captured_requests[-1]
    assert "api-version=2024-06-01" in str(req.url)
    assert "/openai/deployments/gpt-4o-dep/chat/completions" in str(req.url)
    assert req.headers["api-key"] == "azure-secret-key"


def test_presets_coverage():
    """Verify bilingual presets for jais, falcon, and gpt."""
    assert "jais" in PRESETS
    assert "falcon" in PRESETS
    assert "gpt" in PRESETS

    jais = get_preset("jais")
    assert jais["deployment_kind"] == "openai_compatible"
    assert "smoke_prompt_ar" in jais
    assert "smoke_prompt_en" in jais
    assert jais["recommended_max_tokens"] > 0

    falcon = get_preset("falcon")
    assert falcon["model_family"] == "falcon"
    assert "tiiuae" in falcon["default_model"].lower()

    az = get_preset("azure_openai")
    assert az["deployment_kind"] == "azure_openai"
