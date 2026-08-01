"""Forkmark data layer — public facade.

SQLite by default, PostgreSQL via FM_DATABASE_URL. The implementation is split
across core/store_impl/ (per-domain repository mixins) and assembled into the
``Database`` class below. Every name previously importable from ``core.store``
is re-exported here, so this module's public API is unchanged."""
from __future__ import annotations

from core.store_impl.base import *  # noqa: F401,F403
from core.store_impl.base import (  # underscore helpers external code imports
    DatabaseBase,
)
from core.store_impl.stats import StatsMixin
from core.store_impl.testsets import TestSetsMixin
from core.store_impl.evalruns import EvalRunsMixin
from core.store_impl.workflows import WorkflowsMixin
from core.store_impl.branches import BranchesMixin
from core.store_impl.comparisons import ComparisonsMixin
from core.store_impl.decisions import DecisionsMixin
from core.store_impl.apikeys import ApiKeysMixin
from core.store_impl.settings import SettingsMixin
from core.store_impl.providers import ProvidersMixin
from core.store_impl.flywheel import FlywheelMixin
from core.store_impl.collaboration import CollaborationMixin
from core.store_impl.trajectories import TrajectoriesMixin
from core.store_impl.audit import AuditMixin


class Database(
        StatsMixin,
        TestSetsMixin,
        EvalRunsMixin,
        WorkflowsMixin,
        BranchesMixin,
        ComparisonsMixin,
        DecisionsMixin,
        ApiKeysMixin,
        SettingsMixin,
        ProvidersMixin,
        FlywheelMixin,
        CollaborationMixin,
        TrajectoriesMixin,
        AuditMixin,
        DatabaseBase):
    """Forkmark data layer. Assembled from per-domain repository mixins; the
    public API is identical to the previous monolithic core/store.py."""
    pass
