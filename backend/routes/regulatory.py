"""Regulatory framework endpoints.

Exposes the model-validation requirements ForkMark tracks for the CBUAE AI
guidance and Model Management Standards (UAE), the UAE Joint Guidelines, the
EU AI Act (high-risk systems), SR 26-2 (US Fed/OCC/FDIC, superseding SR 11-7),
and PRA SS1/23 (UK), plus per-model artifact coverage against those frameworks.
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.deps import db, ui_read_auth
from core.model_inventory import ModelInventory
from core.regulatory_frameworks import (
    FrameworkRequirements,
    RegulatoryFramework,
    get_all_requirements,
    get_framework_requirements,
)

router = APIRouter(prefix="/api", tags=["regulatory"])


# ── Schemas ─────────────────────────────────────────────────────────────────


class FrameworkRequirementsResponse(BaseModel):
    framework: str = Field(..., description="Framework identifier, e.g. 'cbuae_mms'.")
    name: str
    jurisdiction: str
    reference: str
    summary: str
    required_artifacts: List[str]
    validation_cycle_days: int
    bias_test_required: bool
    human_oversight_required: bool
    documentation_fields: List[str]


class FrameworkCoverageResponse(BaseModel):
    framework: str
    required: List[str]
    present: List[str]
    missing: List[str]
    is_complete: bool


class ModelCoverageResponse(BaseModel):
    model_id: str
    display_name: str
    risk_tier: str
    frameworks: List[FrameworkCoverageResponse]
    overall_complete: bool


def _to_response(req: FrameworkRequirements) -> FrameworkRequirementsResponse:
    return FrameworkRequirementsResponse(
        framework=req.framework.value,
        name=req.name,
        jurisdiction=req.jurisdiction,
        reference=req.reference,
        summary=req.summary,
        required_artifacts=list(req.required_artifacts),
        validation_cycle_days=req.validation_cycle_days,
        bias_test_required=req.bias_test_required,
        human_oversight_required=req.human_oversight_required,
        documentation_fields=list(req.documentation_fields),
    )


# ── Routes ──────────────────────────────────────────────────────────────────


@router.get(
    "/regulatory/frameworks",
    response_model=List[FrameworkRequirementsResponse],
    summary="List supported regulatory frameworks",
)
def list_frameworks(_auth: object = Depends(ui_read_auth)) -> List[FrameworkRequirementsResponse]:
    """Return every supported model risk / AI governance framework and its
    validation requirements: SR 11-7, EU AI Act, PRA SS1/23, and CBUAE."""
    return [_to_response(r) for r in get_all_requirements()]


@router.get(
    "/regulatory/frameworks/{framework_id}",
    response_model=FrameworkRequirementsResponse,
    summary="Get one regulatory framework's requirements",
)
def get_framework(
    framework_id: str, _auth: object = Depends(ui_read_auth)
) -> FrameworkRequirementsResponse:
    """Return the requirements for a single framework (e.g. ``eu_ai_act``)."""
    try:
        framework = RegulatoryFramework(framework_id)
    except ValueError as exc:
        valid = ", ".join(f.value for f in RegulatoryFramework)
        raise HTTPException(404, f"Unknown framework {framework_id!r}. Valid: {valid}") from exc
    return _to_response(get_framework_requirements(framework))


@router.get(
    "/regulatory/models/{model_id}/coverage",
    response_model=ModelCoverageResponse,
    summary="Artifact coverage for a model, per applicable framework",
)
def model_coverage(
    model_id: str, _auth: object = Depends(ui_read_auth)
) -> ModelCoverageResponse:
    """For the given model, report which required evidence artifacts are present
    versus missing under each regulatory framework it is subject to."""
    inventory = ModelInventory(db)
    model = inventory.get_model(model_id)
    if model is None:
        raise HTTPException(404, f"Model not found: {model_id}")

    present = set(model.present_artifacts)
    coverages: List[FrameworkCoverageResponse] = []
    for framework in model.regulatory_frameworks:
        required = get_framework_requirements(framework).required_artifacts
        coverages.append(
            FrameworkCoverageResponse(
                framework=framework.value,
                required=list(required),
                present=[a for a in required if a in present],
                missing=[a for a in required if a not in present],
                is_complete=all(a in present for a in required),
            )
        )
    return ModelCoverageResponse(
        model_id=model.model_id,
        display_name=model.display_name,
        risk_tier=model.risk_tier.value,
        frameworks=coverages,
        overall_complete=all(c.is_complete for c in coverages),
    )
