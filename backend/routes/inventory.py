"""Model inventory endpoints — the system of record for governed models.

Maintaining a current, risk-tiered model inventory is a baseline expectation of
SR 11-7, PRA SS1/23, the EU AI Act, and CBUAE guidance. These endpoints provide
CRUD over that inventory plus the revalidation-due query model risk teams rely on.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.deps import db, principal, ui_read_auth, ui_write_auth
from core.model_inventory import (
    ModelInventory,
    ModelRecord,
    ModelStatus,
    RiskTier,
)
from core.regulatory_frameworks import RegulatoryFramework

router = APIRouter(prefix="/api", tags=["inventory"])

_inventory = ModelInventory(db)


def _audit(action: str, model_id: str, request: Request,
           x_api_key: "str | None", **detail) -> None:
    """Best-effort audit write for inventory mutations."""
    try:
        actor, role = principal(x_api_key)
        ip = request.client.host if request.client else ""
        db.add_audit_log(
            action, actor=actor, actor_role=role, resource_type="model",
            resource_id=model_id, detail=detail, ip=ip,
        )
    except Exception:  # pragma: no cover - auditing must not fail the request
        pass


# ── Schemas ─────────────────────────────────────────────────────────────────


class ModelCreate(BaseModel):
    model_id: str = Field(..., min_length=1, max_length=200)
    display_name: str = Field(..., min_length=1, max_length=300)
    provider: str = ""
    version: str = ""
    use_case: str = ""
    risk_tier: RiskTier = RiskTier.MEDIUM
    regulatory_frameworks: List[RegulatoryFramework] = Field(default_factory=list)
    deployed_at: datetime
    owner_team: str = ""
    documentation_url: str = ""
    status: ModelStatus = ModelStatus.ACTIVE
    last_validated_at: Optional[datetime] = None
    next_validation_due: Optional[datetime] = None
    present_artifacts: List[str] = Field(default_factory=list)
    evaluation_signals: Dict[str, Any] = Field(
        default_factory=dict,
        description="Observed signals for this model. Recognised key: 'bias_groups' "
                    "— a mapping of demographic group to aggregate outcome score, used "
                    "to compute the disparity assessment when a validation memo "
                    "auto-assembles its evidence.",
    )


class ModelUpdate(BaseModel):
    display_name: Optional[str] = None
    provider: Optional[str] = None
    version: Optional[str] = None
    use_case: Optional[str] = None
    risk_tier: Optional[RiskTier] = None
    regulatory_frameworks: Optional[List[RegulatoryFramework]] = None
    deployed_at: Optional[datetime] = None
    owner_team: Optional[str] = None
    documentation_url: Optional[str] = None
    status: Optional[ModelStatus] = None
    last_validated_at: Optional[datetime] = None
    next_validation_due: Optional[datetime] = None
    present_artifacts: Optional[List[str]] = None
    evaluation_signals: Optional[Dict[str, Any]] = Field(
        None,
        description="Observed signals for this model. Recognised key: 'bias_groups' "
                    "— a mapping of demographic group to aggregate outcome score, used "
                    "to compute the disparity assessment when a validation memo "
                    "auto-assembles its evidence.",
    )


class ModelResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    model_id: str
    display_name: str
    provider: str
    version: str
    use_case: str
    risk_tier: str
    regulatory_frameworks: List[str]
    deployed_at: Optional[str]
    owner_team: str
    documentation_url: str
    status: str
    last_validated_at: Optional[str]
    next_validation_due: Optional[str]
    present_artifacts: List[str]
    evaluation_signals: Dict[str, Any] = Field(default_factory=dict)


def _to_response(record: ModelRecord) -> ModelResponse:
    return ModelResponse(**record.to_dict())


# ── Routes ──────────────────────────────────────────────────────────────────


@router.post(
    "/inventory/models",
    status_code=201,
    response_model=ModelResponse,
    summary="Register a model in the inventory",
)
def create_model(body: ModelCreate, request: Request,
                 x_api_key: str = Header(None, alias="X-API-Key"),
                 _auth: object = Depends(ui_write_auth)) -> ModelResponse:
    """Add a model to the governance inventory with its risk tier and the
    regulatory frameworks (SR 11-7 / EU AI Act / PRA SS1/23 / CBUAE) it is
    subject to."""
    record = ModelRecord(
        model_id=body.model_id,
        display_name=body.display_name,
        provider=body.provider,
        version=body.version,
        use_case=body.use_case,
        risk_tier=body.risk_tier,
        regulatory_frameworks=list(body.regulatory_frameworks),
        deployed_at=body.deployed_at,
        owner_team=body.owner_team,
        documentation_url=body.documentation_url,
        status=body.status,
        last_validated_at=body.last_validated_at,
        next_validation_due=body.next_validation_due,
        present_artifacts=list(body.present_artifacts),
        evaluation_signals=dict(body.evaluation_signals),
    )
    try:
        created = _inventory.add_model(record)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    _audit("model.create", created.model_id, request, x_api_key,
           risk_tier=created.risk_tier.value,
           frameworks=[f.value for f in created.regulatory_frameworks])
    return _to_response(created)


@router.get(
    "/inventory/models",
    response_model=List[ModelResponse],
    summary="List models in the inventory",
)
def list_models(
    status: Optional[ModelStatus] = Query(None, description="Filter by lifecycle status"),
    _auth: object = Depends(ui_read_auth),
) -> List[ModelResponse]:
    """List all models, optionally filtered by ACTIVE / UNDER_REVIEW / RETIRED."""
    return [_to_response(m) for m in _inventory.list_models(status=status)]


@router.get(
    "/inventory/models/due-for-revalidation",
    response_model=List[ModelResponse],
    summary="Models due for revalidation within a window",
)
def due_for_revalidation(
    days_ahead: int = Query(30, ge=0, le=3650),
    _auth: object = Depends(ui_read_auth),
) -> List[ModelResponse]:
    """Return ACTIVE models whose next revalidation falls within ``days_ahead``
    days (never-validated models are treated as due)."""
    return [_to_response(m) for m in _inventory.get_due_for_revalidation(days_ahead)]


@router.get(
    "/inventory/models/{model_id}",
    response_model=ModelResponse,
    summary="Get a single model record",
)
def get_model(model_id: str, _auth: object = Depends(ui_read_auth)) -> ModelResponse:
    """Retrieve one model's inventory record."""
    model = _inventory.get_model(model_id)
    if model is None:
        raise HTTPException(404, f"Model not found: {model_id}")
    return _to_response(model)


@router.patch(
    "/inventory/models/{model_id}",
    response_model=ModelResponse,
    summary="Update a model record",
)
def update_model(
    model_id: str, body: ModelUpdate, request: Request,
    x_api_key: str = Header(None, alias="X-API-Key"),
    _auth: object = Depends(ui_write_auth),
) -> ModelResponse:
    """Patch a model record. Only supplied fields are changed; setting
    ``last_validated_at`` re-derives the next revalidation due date."""
    changes: dict[str, Any] = {name: getattr(body, name) for name in body.model_fields_set}
    try:
        updated = _inventory.update_model(model_id, **changes)
    except KeyError as exc:
        raise HTTPException(404, f"Model not found: {model_id}") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    _audit("model.update", model_id, request, x_api_key,
           fields=sorted(changes.keys()))
    return _to_response(updated)


@router.delete(
    "/inventory/models/{model_id}",
    status_code=204,
    summary="Delete a model record",
)
def delete_model(model_id: str, request: Request,
                 x_api_key: str = Header(None, alias="X-API-Key"),
                 _auth: object = Depends(ui_write_auth)) -> None:
    """Remove a model from the inventory."""
    if not _inventory.delete_model(model_id):
        raise HTTPException(404, f"Model not found: {model_id}")
    _audit("model.delete", model_id, request, x_api_key)
    return None
