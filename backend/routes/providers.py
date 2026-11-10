"""LLM provider management endpoints."""
from __future__ import annotations

import time
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from typing import Optional, List

from backend.deps import db, ui_read_auth, ui_write_auth

router = APIRouter(prefix="/api", tags=["providers"])


# ── Schemas ─────────────────────────────────────────────────────────────────

class ProviderCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    provider_type: str = Field(default="openai")
    base_url: str = ""
    api_key: str = ""
    is_default: bool = False
    deployment_kind: Optional[str] = None
    model_family: Optional[str] = ""
    default_model: Optional[str] = ""
    azure_deployment: Optional[str] = ""
    api_version: Optional[str] = ""
    azure_deployment_type: Optional[str] = ""
    inference_region: Optional[str] = ""
    hosting: Optional[str] = "bank_self_hosted"
    vendor_name: Optional[str] = ""
    licence: Optional[str] = ""
    max_concurrency: Optional[int] = 4
    timeout_s: Optional[float] = 60.0
    rpm_limit: Optional[int] = 120
    rpd_limit: Optional[int] = 100000
    max_input_tokens: Optional[int] = 4096

class ProviderUpdate(BaseModel):
    name: Optional[str] = None
    provider_type: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None      # empty string = keep existing key
    is_default: Optional[bool] = None
    deployment_kind: Optional[str] = None
    model_family: Optional[str] = None
    default_model: Optional[str] = None
    azure_deployment: Optional[str] = None
    api_version: Optional[str] = None
    azure_deployment_type: Optional[str] = None
    inference_region: Optional[str] = None
    hosting: Optional[str] = None
    vendor_name: Optional[str] = None
    licence: Optional[str] = None
    max_concurrency: Optional[int] = None
    timeout_s: Optional[float] = None
    rpm_limit: Optional[int] = None
    rpd_limit: Optional[int] = None
    max_input_tokens: Optional[int] = None

class ProviderResponse(BaseModel):
    id: str
    name: str
    provider_type: str = "openai"
    base_url: str = ""
    api_key_set: bool = False
    api_key_masked: str = ""
    is_default: bool = False
    created_at: str
    updated_at: str
    deployment_kind: Optional[str] = "openai_compatible"
    model_family: Optional[str] = ""
    default_model: Optional[str] = ""
    azure_deployment: Optional[str] = ""
    api_version: Optional[str] = ""
    azure_deployment_type: Optional[str] = ""
    inference_region: Optional[str] = ""
    hosting: Optional[str] = "bank_self_hosted"
    vendor_name: Optional[str] = ""
    licence: Optional[str] = ""
    max_concurrency: Optional[int] = 4
    timeout_s: Optional[float] = 60.0
    rpm_limit: Optional[int] = 120
    rpd_limit: Optional[int] = 100000
    max_input_tokens: Optional[int] = 4096

class ProviderTestResult(BaseModel):
    ok: bool
    message: str = ""
    latency_ms: int = 0
    model_reported: str = ""
    system_fingerprint: str = ""
    arabic_ok: bool = False


# ── Valid provider types ────────────────────────────────────────────────────

_VALID_TYPES = {"openai", "anthropic", "openrouter", "ollama", "custom", "azure_openai", "vllm", "tgi", "openai_compatible"}


def _is_arabic_text(text: str) -> bool:
    """True when text is at least 50% Arabic script (non-whitespace)."""
    clean = "".join(text.split())
    if not clean:
        return False
    arabic_chars = sum(
        1 for c in clean if (
            '\u0600' <= c <= '\u06FF' or
            '\u0750' <= c <= '\u077F' or
            '\u08A0' <= c <= '\u08FF' or
            '\uFB50' <= c <= '\uFDFF' or
            '\uFE70' <= c <= '\uFEFF'
        )
    )
    return (arabic_chars / len(clean)) >= 0.5


# ── Routes ──────────────────────────────────────────────────────────────────

@router.get("/providers", response_model=List[ProviderResponse])
def list_providers(_auth=Depends(ui_read_auth)):
    # Auto-migrate legacy key on first access
    db.migrate_legacy_provider()
    return db.list_providers()


@router.post("/providers", status_code=201, response_model=ProviderResponse)
def create_provider(body: ProviderCreate, _auth=Depends(ui_write_auth)):
    ptype = body.provider_type.lower().strip()
    if ptype not in _VALID_TYPES:
        raise HTTPException(400,
            f"Invalid provider_type: {ptype!r}. "
            f"Must be one of: {', '.join(sorted(_VALID_TYPES))}")
    result = db.create_provider(
        name=body.name, provider_type=ptype,
        base_url=body.base_url.strip(), api_key=body.api_key,
        is_default=body.is_default,
        deployment_kind=body.deployment_kind,
        model_family=body.model_family or "",
        default_model=body.default_model or "",
        azure_deployment=body.azure_deployment or "",
        api_version=body.api_version or "",
        azure_deployment_type=body.azure_deployment_type or "",
        inference_region=body.inference_region or "",
        hosting=body.hosting or "bank_self_hosted",
        vendor_name=body.vendor_name or "",
        licence=body.licence or "",
        max_concurrency=body.max_concurrency or 4,
        timeout_s=body.timeout_s or 60.0,
        rpm_limit=body.rpm_limit or 120,
        rpd_limit=body.rpd_limit or 100000,
        max_input_tokens=body.max_input_tokens or 4096,
    )
    # Re-fetch to get masked key fields
    return db.get_provider(result["id"]) or result


@router.patch("/providers/{provider_id}", response_model=ProviderResponse)
def update_provider(provider_id: str, body: ProviderUpdate,
                    _auth=Depends(ui_write_auth)):
    kwargs = {}
    if body.name is not None:
        kwargs["name"] = body.name
    if body.provider_type is not None:
        ptype = body.provider_type.lower().strip()
        if ptype not in _VALID_TYPES:
            raise HTTPException(400, f"Invalid provider_type: {ptype!r}")
        kwargs["provider_type"] = ptype
    if body.base_url is not None:
        kwargs["base_url"] = body.base_url.strip()
    if body.api_key is not None:
        kwargs["api_key"] = body.api_key
    if body.is_default is not None:
        kwargs["is_default"] = body.is_default
    if body.deployment_kind is not None:
        kwargs["deployment_kind"] = body.deployment_kind
    if body.model_family is not None:
        kwargs["model_family"] = body.model_family
    if body.default_model is not None:
        kwargs["default_model"] = body.default_model
    if body.azure_deployment is not None:
        kwargs["azure_deployment"] = body.azure_deployment
    if body.api_version is not None:
        kwargs["api_version"] = body.api_version
    if body.azure_deployment_type is not None:
        kwargs["azure_deployment_type"] = body.azure_deployment_type
    if body.inference_region is not None:
        kwargs["inference_region"] = inference_region = body.inference_region
    if body.hosting is not None:
        kwargs["hosting"] = body.hosting
    if body.vendor_name is not None:
        kwargs["vendor_name"] = body.vendor_name
    if body.licence is not None:
        kwargs["licence"] = body.licence
    if body.max_concurrency is not None:
        kwargs["max_concurrency"] = body.max_concurrency
    if body.timeout_s is not None:
        kwargs["timeout_s"] = body.timeout_s
    if body.rpm_limit is not None:
        kwargs["rpm_limit"] = body.rpm_limit
    if body.rpd_limit is not None:
        kwargs["rpd_limit"] = body.rpd_limit
    if body.max_input_tokens is not None:
        kwargs["max_input_tokens"] = body.max_input_tokens

    result = db.update_provider(provider_id, **kwargs)
    if result is None:
        raise HTTPException(404, "Provider not found")
    return result


@router.delete("/providers/{provider_id}", status_code=204)
def delete_provider(provider_id: str, _auth=Depends(ui_write_auth)):
    ok = db.delete_provider(provider_id)
    if not ok:
        raise HTTPException(404, "Provider not found")


@router.post("/providers/{provider_id}/test", response_model=ProviderTestResult)
def test_provider(provider_id: str, _auth=Depends(ui_write_auth)):
    """Test a provider's connection by making bilingual test calls through the gateway."""
    creds = db.get_provider_credentials(provider_id)
    if creds is None:
        raise HTTPException(404, "Provider not found")
    if not creds.get("api_key"):
        return {
            "ok": False,
            "message": "No API key configured for this provider",
            "latency_ms": 0,
            "model_reported": "",
            "system_fingerprint": "",
            "arabic_ok": False,
        }

    from core.llm_gateway import gateway
    from core.egress_policy import EgressBlocked

    try:
        t0 = time.time()
        # English prompt
        en_resp = gateway.chat(
            connector_id=provider_id,
            messages=[{"role": "user", "content": "Hello, provide a brief test response."}],
            max_tokens=32,
            purpose="provider_test",
        )
        # Arabic prompt
        ar_resp = gateway.chat(
            connector_id=provider_id,
            messages=[{"role": "user", "content": "مرحبا، هل يمكنك تقديم رد قصير؟"}],
            max_tokens=32,
            purpose="provider_test",
        )
        latency_ms = int((time.time() - t0) * 1000)
        ar_ok = _is_arabic_text(ar_resp.text)

        if en_resp.model_reported:
            try:
                db.record_fingerprint(
                    governed_model_id="",
                    connector_id=provider_id,
                    model_reported=en_resp.model_reported,
                    system_fingerprint=en_resp.system_fingerprint,
                    source="provider_test",
                )
            except Exception:
                pass

        return {
            "ok": True,
            "message": f"Connection successful ({latency_ms}ms)",
            "latency_ms": latency_ms,
            "model_reported": en_resp.model_reported,
            "system_fingerprint": en_resp.system_fingerprint,
            "arabic_ok": ar_ok,
        }
    except EgressBlocked as e:
        raise HTTPException(403, str(e))
    except Exception as e:
        return {
            "ok": False,
            "message": f"Connection failed: {str(e)[:200]}",
            "latency_ms": 0,
            "model_reported": "",
            "system_fingerprint": "",
            "arabic_ok": False,
        }
