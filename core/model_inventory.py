"""Model inventory for ForkMark — the system of record for models under governance.

A regulated firm must maintain a complete, current inventory of the models it
uses, each tagged with its risk tier, owning team, the regulatory frameworks it
falls under, and its validation status (SR 11-7 Principle: model inventory;
PRA SS1/23 Principle 1: model identification). This module provides that record
plus the two queries validators reach for most often: what is due for
revalidation, and where is our evidence incomplete.

Persistence uses the existing ForkMark data layer (``core.store``). The backing
``model_inventory`` table is created by store migration v9 (see
``core.store_impl.base``), with a matching Alembic revision under
``migrations/versions/`` for shared-schema deployments.
"""
from __future__ import annotations

import json
import logging
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional, Protocol

from core.regulatory_frameworks import (
    RegulatoryFramework,
    get_framework_requirements,
)

__all__ = [
    "RiskTier",
    "ModelStatus",
    "ModelRecord",
    "FrameworkCoverage",
    "ModelCoverage",
    "ModelInventory",
]

logger = logging.getLogger("forkmark.model_inventory")

# Fallback revalidation cadence if a model has no frameworks attached.
_DEFAULT_CYCLE_DAYS = 365


class RiskTier(str, Enum):
    """Model risk tier, driving validation intensity and oversight."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ModelStatus(str, Enum):
    """Lifecycle status of a model in the inventory."""

    ACTIVE = "ACTIVE"
    UNDER_REVIEW = "UNDER_REVIEW"
    RETIRED = "RETIRED"


# ── Row-access helpers ────────────────────────────────────────────────────────


class _Conn(Protocol):
    """Structural type for a ForkMark DB connection wrapper (SQLite/PostgreSQL)."""

    def execute(self, sql: str, params: "tuple[Any, ...]" = ...) -> Any: ...
    def fetchone(self, sql: str, params: "tuple[Any, ...]" = ...) -> Any: ...
    def fetchall(self, sql: str, params: "tuple[Any, ...]" = ...) -> Any: ...


class SupportsStore(Protocol):
    """Structural type for the ForkMark ``Database`` (only the parts we use)."""

    def _conn(self) -> AbstractContextManager[_Conn]: ...
    def _read_conn(self) -> AbstractContextManager[_Conn]: ...


def _as_dict(row: Any) -> dict[str, Any]:
    """Convert a SQLite ``Row`` or PostgreSQL dict row into a plain dict."""
    return dict(row)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ModelRecord:
    """A single governed model and its validation metadata.

    Attributes:
        model_id:              Stable unique identifier (primary key).
        display_name:          Human-readable model name.
        provider:              Model provider (e.g. "openai", "anthropic", "in-house").
        version:               Model/version string.
        use_case:              Business use case the model serves.
        risk_tier:             Risk classification (drives validation intensity).
        regulatory_frameworks: Frameworks the model must comply with.
        deployed_at:           When the model went into production use.
        owner_team:            Accountable owning team.
        documentation_url:     Link to the model's documentation / model card.
        status:                Lifecycle status.
        last_validated_at:     Date of the most recent full validation, if any.
        next_validation_due:   When the next revalidation is due; auto-derived from
                               ``last_validated_at`` and the shortest applicable
                               framework cycle when not set explicitly.
        present_artifacts:     Evidence artifacts currently on file for this model
                               (values from ``ArtifactType``); drives coverage.
    """

    model_id: str
    display_name: str
    provider: str
    version: str
    use_case: str
    risk_tier: RiskTier
    regulatory_frameworks: list[RegulatoryFramework]
    deployed_at: datetime
    owner_team: str
    documentation_url: str = ""
    status: ModelStatus = ModelStatus.ACTIVE
    last_validated_at: Optional[datetime] = None
    next_validation_due: Optional[datetime] = None
    present_artifacts: list[str] = field(default_factory=list)
    # Observed evaluation signals for this model (e.g. per-group fairness scores),
    # ingested from validation runs; used to auto-assemble the validation memo.
    evaluation_signals: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict (enums -> values, datetimes -> ISO)."""
        return {
            "model_id": self.model_id,
            "display_name": self.display_name,
            "provider": self.provider,
            "version": self.version,
            "use_case": self.use_case,
            "risk_tier": self.risk_tier.value,
            "regulatory_frameworks": [f.value for f in self.regulatory_frameworks],
            "deployed_at": _iso(self.deployed_at),
            "owner_team": self.owner_team,
            "documentation_url": self.documentation_url,
            "status": self.status.value,
            "last_validated_at": _iso(self.last_validated_at),
            "next_validation_due": _iso(self.next_validation_due),
            "present_artifacts": list(self.present_artifacts),
            "evaluation_signals": dict(self.evaluation_signals),
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ModelRecord":
        """Build a record from a database row dict."""
        deployed = _parse_dt(row["deployed_at"])
        if deployed is None:  # deployed_at is NOT NULL in the schema
            raise ValueError(f"model {row.get('model_id')!r} has no deployed_at")
        return cls(
            model_id=row["model_id"],
            display_name=row["display_name"],
            provider=row["provider"],
            version=row["version"],
            use_case=row["use_case"],
            risk_tier=RiskTier(row["risk_tier"]),
            regulatory_frameworks=[
                RegulatoryFramework(v)
                for v in json.loads(row["regulatory_frameworks"] or "[]")
            ],
            deployed_at=deployed,
            owner_team=row["owner_team"],
            documentation_url=row["documentation_url"],
            status=ModelStatus(row["status"]),
            last_validated_at=_parse_dt(row.get("last_validated_at")),
            next_validation_due=_parse_dt(row.get("next_validation_due")),
            present_artifacts=list(json.loads(row["present_artifacts"] or "[]")),
            evaluation_signals=json.loads(row.get("evaluation_signals") or "{}"),
        )


@dataclass(frozen=True)
class FrameworkCoverage:
    """Artifact coverage for one framework applied to one model."""

    framework: RegulatoryFramework
    required: list[str]
    present: list[str]
    missing: list[str]
    is_complete: bool


@dataclass(frozen=True)
class ModelCoverage:
    """Aggregate compliance-artifact coverage for a single model."""

    model_id: str
    display_name: str
    risk_tier: RiskTier
    frameworks: list[FrameworkCoverage]
    overall_complete: bool


def compute_next_validation_due(
    last_validated_at: Optional[datetime],
    frameworks: list[RegulatoryFramework],
) -> Optional[datetime]:
    """Derive the next revalidation date from the last validation and the
    shortest (most conservative) applicable framework cycle."""
    if last_validated_at is None:
        return None
    cycles = [
        get_framework_requirements(f).validation_cycle_days for f in frameworks
    ]
    days = min(cycles) if cycles else _DEFAULT_CYCLE_DAYS
    return last_validated_at + timedelta(days=days)


class ModelInventory:
    """CRUD repository and governance queries over the ``model_inventory`` table.

    Args:
        db: A ForkMark ``Database`` (or anything satisfying :class:`SupportsStore`).
    """

    def __init__(self, db: SupportsStore) -> None:
        self._db = db

    # ── Create ────────────────────────────────────────────────────────────────

    def add_model(self, record: ModelRecord) -> ModelRecord:
        """Insert a new model record.

        Auto-derives ``next_validation_due`` when a model has been validated but no
        due date was supplied.

        Raises:
            ValueError: If a model with the same ``model_id`` already exists.
        """
        if record.next_validation_due is None and record.last_validated_at is not None:
            record.next_validation_due = compute_next_validation_due(
                record.last_validated_at, record.regulatory_frameworks
            )
        now = _now().isoformat()
        try:
            with self._db._conn() as c:
                c.execute(
                    """INSERT INTO model_inventory (
                        model_id, display_name, provider, version, use_case,
                        risk_tier, regulatory_frameworks, deployed_at,
                        last_validated_at, next_validation_due, owner_team,
                        documentation_url, status, present_artifacts,
                        evaluation_signals, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        record.model_id,
                        record.display_name,
                        record.provider,
                        record.version,
                        record.use_case,
                        record.risk_tier.value,
                        json.dumps([f.value for f in record.regulatory_frameworks]),
                        record.deployed_at.isoformat(),
                        _iso(record.last_validated_at),
                        _iso(record.next_validation_due),
                        record.owner_team,
                        record.documentation_url,
                        record.status.value,
                        json.dumps(list(record.present_artifacts)),
                        json.dumps(dict(record.evaluation_signals)),
                        now,
                        now,
                    ),
                )
        except Exception as exc:  # narrow: only the duplicate-key case is expected
            if self.get_model(record.model_id) is not None:
                raise ValueError(
                    f"model_id already exists: {record.model_id!r}"
                ) from exc
            raise
        logger.info("model inventory: added %s (%s)", record.model_id, record.risk_tier)
        return record

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_model(self, model_id: str) -> Optional[ModelRecord]:
        """Return the record for ``model_id`` or ``None`` if absent."""
        with self._db._read_conn() as c:
            row = c.fetchone(
                "SELECT * FROM model_inventory WHERE model_id = ?", (model_id,)
            )
        return ModelRecord.from_row(_as_dict(row)) if row is not None else None

    def list_models(
        self, status: Optional[ModelStatus] = None
    ) -> list[ModelRecord]:
        """List all models, optionally filtered by ``status``."""
        with self._db._read_conn() as c:
            if status is None:
                rows = c.fetchall(
                    "SELECT * FROM model_inventory ORDER BY display_name"
                )
            else:
                rows = c.fetchall(
                    "SELECT * FROM model_inventory WHERE status = ? "
                    "ORDER BY display_name",
                    (status.value,),
                )
        return [ModelRecord.from_row(_as_dict(r)) for r in rows]

    # ── Update ────────────────────────────────────────────────────────────────

    def update_model(self, model_id: str, **changes: Any) -> ModelRecord:
        """Patch a model record with the supplied field changes.

        Recomputes ``next_validation_due`` when ``last_validated_at`` changes and no
        explicit due date is supplied in the same call.

        Raises:
            KeyError:   If the model does not exist.
            ValueError: If an unknown field is supplied.
        """
        record = self.get_model(model_id)
        if record is None:
            raise KeyError(f"model not found: {model_id!r}")

        allowed = set(ModelRecord.__dataclass_fields__.keys()) - {"model_id"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unknown field(s): {sorted(unknown)}")

        for key, value in changes.items():
            setattr(record, key, value)
        if "last_validated_at" in changes and "next_validation_due" not in changes:
            record.next_validation_due = compute_next_validation_due(
                record.last_validated_at, record.regulatory_frameworks
            )

        with self._db._conn() as c:
            c.execute(
                """UPDATE model_inventory SET
                    display_name = ?, provider = ?, version = ?, use_case = ?,
                    risk_tier = ?, regulatory_frameworks = ?, deployed_at = ?,
                    last_validated_at = ?, next_validation_due = ?, owner_team = ?,
                    documentation_url = ?, status = ?, present_artifacts = ?,
                    evaluation_signals = ?, updated_at = ?
                   WHERE model_id = ?""",
                (
                    record.display_name,
                    record.provider,
                    record.version,
                    record.use_case,
                    record.risk_tier.value,
                    json.dumps([f.value for f in record.regulatory_frameworks]),
                    record.deployed_at.isoformat(),
                    _iso(record.last_validated_at),
                    _iso(record.next_validation_due),
                    record.owner_team,
                    record.documentation_url,
                    record.status.value,
                    json.dumps(list(record.present_artifacts)),
                    json.dumps(dict(record.evaluation_signals)),
                    _now().isoformat(),
                    model_id,
                ),
            )
        logger.info("model inventory: updated %s", model_id)
        return record

    # ── Delete ────────────────────────────────────────────────────────────────

    def delete_model(self, model_id: str) -> bool:
        """Delete a model record. Returns ``True`` if a row was removed."""
        existed = self.get_model(model_id) is not None
        with self._db._conn() as c:
            c.execute("DELETE FROM model_inventory WHERE model_id = ?", (model_id,))
        if existed:
            logger.info("model inventory: deleted %s", model_id)
        return existed

    # ── Governance queries ──────────────────────────────────────────────────────

    def get_due_for_revalidation(self, days_ahead: int = 30) -> list[ModelRecord]:
        """Return ACTIVE models whose next revalidation falls within the window.

        Models that have never been validated (``next_validation_due is None``) are
        treated as due and included first. Results are sorted by due date.

        Args:
            days_ahead: Size of the look-ahead window in days (default 30).
        """
        cutoff = _now() + timedelta(days=days_ahead)
        due = [
            m
            for m in self.list_models(status=ModelStatus.ACTIVE)
            if m.next_validation_due is None or m.next_validation_due <= cutoff
        ]
        # None (never validated) sorts first, then by ascending due date.
        due.sort(key=lambda m: (m.next_validation_due is not None, m.next_validation_due or _now()))
        return due

    def compliance_coverage_report(self) -> list[ModelCoverage]:
        """Report artifact coverage for every ACTIVE model, per framework.

        For each active model and each framework it is subject to, computes which
        required artifacts are present versus missing (by comparing the framework's
        ``required_artifacts`` against the model's ``present_artifacts``).
        """
        report: list[ModelCoverage] = []
        for model in self.list_models(status=ModelStatus.ACTIVE):
            present_set = set(model.present_artifacts)
            coverages: list[FrameworkCoverage] = []
            for framework in model.regulatory_frameworks:
                required = get_framework_requirements(framework).required_artifacts
                present = [a for a in required if a in present_set]
                missing = [a for a in required if a not in present_set]
                coverages.append(
                    FrameworkCoverage(
                        framework=framework,
                        required=list(required),
                        present=present,
                        missing=missing,
                        is_complete=not missing,
                    )
                )
            report.append(
                ModelCoverage(
                    model_id=model.model_id,
                    display_name=model.display_name,
                    risk_tier=model.risk_tier,
                    frameworks=coverages,
                    overall_complete=all(c.is_complete for c in coverages),
                )
            )
        return report
