"""Unit tests for core.model_inventory."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.model_inventory import (
    ModelInventory,
    ModelRecord,
    ModelStatus,
    RiskTier,
)
from core.regulatory_frameworks import ArtifactType, RegulatoryFramework
from core.store import Database


@pytest.fixture()
def inventory(tmp_path: Path) -> ModelInventory:
    return ModelInventory(Database(str(tmp_path / "inv.db")))


def _record(model_id: str, **overrides: object) -> ModelRecord:
    base: dict[str, object] = dict(
        model_id=model_id,
        display_name=f"Model {model_id}",
        provider="openai",
        version="1.0",
        use_case="credit adjudication",
        risk_tier=RiskTier.HIGH,
        regulatory_frameworks=[RegulatoryFramework.SR_26_2],
        deployed_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        owner_team="Model Risk",
    )
    base.update(overrides)
    return ModelRecord(**base)  # type: ignore[arg-type]


def test_crud_roundtrip(inventory: ModelInventory) -> None:
    """Add, get, list, update, and delete a model record."""
    inventory.add_model(_record("m1"))
    fetched = inventory.get_model("m1")
    assert fetched is not None
    assert fetched.display_name == "Model m1"
    assert fetched.risk_tier == RiskTier.HIGH
    assert fetched.regulatory_frameworks == [RegulatoryFramework.SR_26_2]

    assert [m.model_id for m in inventory.list_models()] == ["m1"]

    updated = inventory.update_model("m1", status=ModelStatus.UNDER_REVIEW,
                                     risk_tier=RiskTier.CRITICAL)
    assert updated.status == ModelStatus.UNDER_REVIEW
    assert inventory.get_model("m1").risk_tier == RiskTier.CRITICAL  # type: ignore[union-attr]

    assert inventory.delete_model("m1") is True
    assert inventory.get_model("m1") is None
    assert inventory.delete_model("m1") is False


def test_add_duplicate_raises(inventory: ModelInventory) -> None:
    inventory.add_model(_record("dup"))
    with pytest.raises(ValueError):
        inventory.add_model(_record("dup"))


def test_update_validation_dates_are_derived(inventory: ModelInventory) -> None:
    """Setting last_validated_at derives next_validation_due (annual cycle)."""
    inventory.add_model(_record("m2"))
    validated = datetime(2026, 1, 1, tzinfo=timezone.utc)
    updated = inventory.update_model("m2", last_validated_at=validated)
    assert updated.next_validation_due == validated + timedelta(days=365)


def test_update_rejects_unknown_field_and_missing_model(
    inventory: ModelInventory,
) -> None:
    inventory.add_model(_record("m3"))
    with pytest.raises(ValueError):
        inventory.update_model("m3", not_a_field=1)
    with pytest.raises(KeyError):
        inventory.update_model("does-not-exist", display_name="x")


def test_get_due_for_revalidation(inventory: ModelInventory) -> None:
    """Active models due within the window are returned; never-validated first;
    retired models excluded."""
    now = datetime.now(timezone.utc)
    # Not due: validated today, next due ~365 days out.
    inventory.add_model(_record("fresh", last_validated_at=now))
    # Due soon: validated ~350 days ago, next due ~15 days out.
    inventory.add_model(_record("due_soon", last_validated_at=now - timedelta(days=350)))
    # Never validated: treated as due.
    inventory.add_model(_record("never"))
    # Retired and overdue: excluded because it is not ACTIVE.
    inventory.add_model(
        _record("retired", status=ModelStatus.RETIRED,
                last_validated_at=now - timedelta(days=400)),
    )

    due_ids = [m.model_id for m in inventory.get_due_for_revalidation(days_ahead=30)]
    assert "due_soon" in due_ids
    assert "never" in due_ids
    assert "fresh" not in due_ids
    assert "retired" not in due_ids
    # Never-validated (due date None) sorts first.
    assert due_ids[0] == "never"


def test_compliance_coverage_report(inventory: ModelInventory) -> None:
    """Coverage compares present artifacts against each framework's requirements."""
    inventory.add_model(
        _record(
            "partial",
            regulatory_frameworks=[RegulatoryFramework.SR_26_2],
            present_artifacts=[
                ArtifactType.CONCEPTUAL_SOUNDNESS.value,
                ArtifactType.VALIDATION_MEMO.value,
            ],
        )
    )
    inventory.add_model(
        _record(
            "complete",
            regulatory_frameworks=[RegulatoryFramework.SR_26_2],
            present_artifacts=[
                ArtifactType.CONCEPTUAL_SOUNDNESS.value,
                ArtifactType.OUTCOME_ANALYSIS.value,
                ArtifactType.ONGOING_MONITORING.value,
                ArtifactType.HUMAN_OVERSIGHT.value,
                ArtifactType.VALIDATION_MEMO.value,
            ],
        )
    )
    inventory.add_model(_record("retired", status=ModelStatus.RETIRED))

    report = {c.model_id: c for c in inventory.compliance_coverage_report()}
    # Retired model excluded.
    assert set(report) == {"partial", "complete"}

    partial = report["partial"].frameworks[0]
    assert partial.framework == RegulatoryFramework.SR_26_2
    assert ArtifactType.OUTCOME_ANALYSIS.value in partial.missing
    assert partial.is_complete is False
    assert report["partial"].overall_complete is False

    assert report["complete"].frameworks[0].is_complete is True
    assert report["complete"].overall_complete is True


def test_record_to_dict_is_json_friendly(inventory: ModelInventory) -> None:
    rec = _record("m4", regulatory_frameworks=[RegulatoryFramework.EU_AI_ACT])
    d = rec.to_dict()
    assert d["risk_tier"] == "HIGH"
    assert d["regulatory_frameworks"] == ["eu_ai_act"]
    assert d["status"] == "ACTIVE"
    assert d["deployed_at"].startswith("2025-01-01")
