"""Model inventory endpoints — the system of record for governed models.

Maintaining a current, risk-tiered model inventory is a baseline expectation of
SR 11-7, PRA SS1/23, the EU AI Act, and CBUAE guidance. These endpoints provide
CRUD over that inventory plus the revalidation-due query model risk teams rely on.
"""
from __future__ import annotations

import logging
import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

_BACKGROUND_TASKS: Set[asyncio.Task] = set()

from backend.deps import db, principal, ui_read_auth, ui_write_auth
from core.model_inventory import (
    ModelInventory,
    ModelRecord,
    ModelStatus,
    RiskTier,
)
from core.regulatory_frameworks import RegulatoryFramework

logger = logging.getLogger("forkmark.inventory")
router = APIRouter(prefix="/api", tags=["inventory"])

_inventory = ModelInventory(db)


def _audit(action: str, model_id: str, request: Request,
           x_api_key: "str | None", **detail) -> None:
    """Mandatory supervisory audit write. Refuses action if audit trail cannot be secured."""
    try:
        actor, role = principal(x_api_key)
        ip = request.client.host if request.client else ""
        db.add_audit_log(
            action, actor=actor, actor_role=role, resource_type="model",
            resource_id=model_id, detail=detail, ip=ip,
        )
    except Exception as exc:
        logger.error("Audit logging failed for %s on model %s: %s", action, model_id, exc)
        raise HTTPException(
            500,
            f"Audit trail write failed for {action}: supervisory action refused to preserve regulatory compliance ({exc})",
        ) from exc


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
    connector_id: Optional[str] = ""


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
    connector_id: Optional[str] = None
    due_diligence_reviewed: Optional[bool] = None


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
    connector_id: Optional[str] = ""


class ValidationRunBranch(BaseModel):
    connector_id: Optional[str] = None
    model: str = "default"
    temperature: float = 0.0
    system_prompt: Optional[str] = None


class ValidationRunRequest(BaseModel):
    test_set_id: str
    champion: ValidationRunBranch
    challenger: Optional[ValidationRunBranch] = None
    suites: List[str] = Field(default_factory=lambda: ["ab"])
    evaluators: List[Dict[str, Any]] = Field(default_factory=list)


class ValidationRunResponse(BaseModel):
    eval_run_ids: List[str]


class FingerprintResponse(BaseModel):
    id: str
    governed_model_id: str
    connector_id: str
    observed_at: str
    model_reported: str
    system_fingerprint: str = ""
    source: str
    eval_run_id: Optional[str] = None


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
        connector_id=body.connector_id or "",
    )
    actor, role = principal(x_api_key)
    ip = request.client.host if request.client else ""
    try:
        created = _inventory.add_model(record, audit_actor=actor, audit_role=role, ip=ip)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        logger.error("Audit logging failed during model creation for %s: %s", record.model_id, exc)
        raise HTTPException(
            500,
            f"Audit trail write failed for model.create: supervisory action refused to preserve regulatory compliance ({exc})",
        ) from exc
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
    actor, role = principal(x_api_key)
    ip = request.client.host if request.client else ""
    changes: dict[str, Any] = {name: getattr(body, name) for name in body.model_fields_set}
    if "due_diligence_reviewed" in changes:
        dd = changes.pop("due_diligence_reviewed")
        if dd:
            curr = _inventory.get_model(model_id)
            arts = set(changes.get("present_artifacts") or (curr.present_artifacts if curr else []))
            arts.add("vendor_due_diligence_record")
            changes["present_artifacts"] = list(arts)
            sigs = dict(changes.get("evaluation_signals") or (curr.evaluation_signals if curr else {}))
            sigs["due_diligence_reviewed"] = True
            changes["evaluation_signals"] = sigs
    try:
        updated = _inventory.update_model(
            model_id, audit_actor=actor, audit_role=role, ip=ip, **changes
        )
    except KeyError as exc:
        raise HTTPException(404, f"Model not found: {model_id}") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logger.error("Audit logging failed during model update for %s: %s", model_id, exc)
        raise HTTPException(
            500,
            f"Audit trail write failed for model.update: supervisory action refused to preserve regulatory compliance ({exc})",
        ) from exc
    return _to_response(updated)


@router.delete(
    "/inventory/models/{model_id}",
    status_code=204,
    summary="Delete a model record",
)
def delete_model(model_id: str, request: Request,
                 x_api_key: str = Header(None, alias="X-API-Key"),
                 _auth: object = Depends(ui_write_auth)) -> None:
    """Remove a model from the inventory.

    The deletion and its supervisory audit log entry are committed atomically
    in the same database transaction. If audit log persistence fails, the model
    deletion is rolled back and refused.
    """
    actor, role = principal(x_api_key)
    ip = request.client.host if request.client else ""
    if _inventory.get_model(model_id) is None:
        raise HTTPException(404, f"Model not found: {model_id}")
    try:
        if not _inventory.delete_model(model_id, audit_actor=actor, audit_role=role, ip=ip):
            raise HTTPException(404, f"Model not found: {model_id}")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Audit logging failed during model delete for %s: %s", model_id, exc)
        raise HTTPException(
            500,
            f"Audit trail write failed for model.delete: supervisory action refused to preserve regulatory compliance ({exc})",
        ) from exc
    return None


@router.post(
    "/inventory/models/{model_id}/validation-runs",
    status_code=202,
    response_model=ValidationRunResponse,
    summary="Trigger validation runs for a governed model",
)
async def trigger_validation_runs(
    model_id: str,
    body: ValidationRunRequest,
    request: Request,
    x_api_key: str = Header(None, alias="X-API-Key"),
    _auth: object = Depends(ui_write_auth),
) -> ValidationRunResponse:
    """Trigger background validation runs for a governed model across one or more test suites."""
    record = _inventory.get_model(model_id)
    if record is None:
        raise HTTPException(404, f"Model not found: {model_id}")

    raw_cases = db.list_test_cases(body.test_set_id)
    if not raw_cases:
        raise HTTPException(400, f"Test set {body.test_set_id} not found or contains no test cases")

    test_cases = [tc.to_dict() if hasattr(tc, "to_dict") else dict(tc) for tc in raw_cases]

    champ_dict = body.champion.model_dump()
    if not champ_dict.get("connector_id"):
        champ_dict["connector_id"] = record.connector_id
    if not champ_dict.get("connector_id"):
        def_creds = db.get_default_provider_credentials()
        if def_creds:
            champ_dict["connector_id"] = def_creds.get("provider_id")

    chal_dict = body.challenger.model_dump() if body.challenger else None

    # Egress pre-validation
    from core.llm_gateway import gateway
    from core.egress_policy import EgressBlocked
    try:
        conn_a = gateway._resolve_connection(champ_dict.get("connector_id"))
        gateway._check_and_log_egress(conn_a, champ_dict.get("model", "default"), "validation_run", None, "system")
        if chal_dict and chal_dict.get("connector_id"):
            conn_b = gateway._resolve_connection(chal_dict.get("connector_id"))
            gateway._check_and_log_egress(conn_b, chal_dict.get("model", "default"), "validation_run", None, "system")
    except EgressBlocked as e:
        raise HTTPException(403, str(e))

    wf = db.upsert_workflow(f"validation_{model_id}")
    eval_run_ids = []

    from core.suite_runner import run_suite_async

    suites = body.suites or ["ab"]
    for suite in suites:
        er_name = f"{record.display_name} — {suite.upper()} validation"
        er = db.create_eval_run(
            workflow_id=wf.id,
            name=er_name,
            branch_a_config=champ_dict,
            branch_b_config=chal_dict or {},
            description=f"{suite.capitalize()} suite validation run for model {model_id}",
            test_set_id=body.test_set_id,
            total_cases=len(test_cases),
            governed_model_id=model_id,
            run_kind=suite,
        )
        eval_run_ids.append(er.id)

        task = asyncio.create_task(
            run_suite_async(
                db=db,
                eval_run_id=er.id,
                workflow_id=wf.id,
                test_cases=test_cases,
                champion=champ_dict,
                challenger=chal_dict,
                governed_model_id=model_id,
                run_kind=suite,
                evaluators=body.evaluators,
            )
        )
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)

    actor, role = principal(x_api_key)
    try:
        db.add_audit_log(
            action="model.validation_run_triggered",
            actor=actor,
            actor_role=role,
            resource_type="governed_model",
            resource_id=model_id,
            detail={"suites": suites, "eval_run_ids": eval_run_ids, "test_set_id": body.test_set_id},
        )
    except Exception as exc:
        logger.warning("Failed to record audit log for validation run: %s", exc)

    return ValidationRunResponse(eval_run_ids=eval_run_ids)


@router.get(
    "/inventory/models/{model_id}/fingerprints",
    response_model=List[FingerprintResponse],
    summary="List observed fingerprints for a governed model",
)
def list_model_fingerprints(
    model_id: str,
    limit: int = Query(50, ge=1, le=500),
    _auth: object = Depends(ui_read_auth),
) -> List[FingerprintResponse]:
    """List hardware and model provenance fingerprints observed for this governed model."""
    record = _inventory.get_model(model_id)
    if record is None:
        raise HTTPException(404, f"Model not found: {model_id}")
    fps = db.list_fingerprints(governed_model_id=model_id, limit=limit)
    return [FingerprintResponse(**fp) for fp in fps]
