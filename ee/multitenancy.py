"""Multi-tenancy layer — workspace schema isolation.

Each workspace gets its own PostgreSQL schema. The control plane (orgs, users,
API keys, audit log) lives in the public schema.

Architecture:
    public schema:    organizations, users, workspaces, api_keys, audit_log
    workspace_{id}:   workflows, workflow_runs, branches, step_outputs,
                      comparisons, decisions, test_sets, test_cases, eval_runs

Data isolation is enforced at TWO levels:
    1. Schema isolation: SET search_path = workspace_{id}
       → queries physically cannot access other workspace tables
    2. RLS on control plane: app.current_org_id session variable
       → even shared tables are filtered per-org

Usage:
    from ee.multitenancy import workspace_provisioner, WorkspaceContext

    # Create a workspace (triggered by SCIM or admin action)
    ws_id = await workspace_provisioner.create_workspace(org_id, "Fraud Detection", user_id)

    # Get a scoped connection for request handling
    ctx = workspace_provisioner.get_context(workspace_id, org_id)
    with ctx.connect() as conn:
        conn.execute("SELECT * FROM workflows")  # hits workspace schema only
"""

from __future__ import annotations

import logging
import re
import time
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

logger = logging.getLogger("forkmark.multitenancy")


# ---------------------------------------------------------------------------
# Data models for the control plane
# ---------------------------------------------------------------------------

@dataclass
class Organization:
    id: str
    name: str
    domain: str                     # e.g. "acmecorp.com"
    created_at: str = ""
    settings: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "domain": self.domain,
            "created_at": self.created_at,
            "settings": self.settings,
        }


@dataclass
class Workspace:
    id: str
    org_id: str
    name: str
    schema_name: str
    is_sandbox: bool = False
    created_by: str = ""
    created_at: str = ""
    deleted_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "org_id": self.org_id,
            "name": self.name,
            "schema_name": self.schema_name,
            "is_sandbox": self.is_sandbox,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "deleted_at": self.deleted_at,
        }


@dataclass
class WorkspaceMembership:
    user_id: str
    workspace_id: str
    org_id: str
    role: str = "evaluator"         # org_admin, ws_admin, evaluator, viewer, sdk_only
    created_at: str = ""


# ---------------------------------------------------------------------------
# Workspace DDL template (applied to each new workspace schema)
# ---------------------------------------------------------------------------

WORKSPACE_SCHEMA_DDL = """
-- Workflows
CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    config TEXT DEFAULT '{}',
    eval_run_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Workflow runs
CREATE TABLE IF NOT EXISTS workflow_runs (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    eval_run_id TEXT,
    test_case_label TEXT DEFAULT '',
    run_type TEXT DEFAULT 'standard',
    status TEXT NOT NULL DEFAULT 'running',
    config TEXT DEFAULT '{}',
    metadata TEXT DEFAULT '{}',
    started_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_workflow ON workflow_runs(workflow_id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON workflow_runs(status);

-- Branches (A/B variants within a run)
CREATE TABLE IF NOT EXISTS branches (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT 'main',
    provider_id TEXT,
    metadata TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_branches_run ON branches(run_id);

-- Step outputs (individual LLM call results)
CREATE TABLE IF NOT EXISTS step_outputs (
    id TEXT PRIMARY KEY,
    branch_id TEXT NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    step_index INTEGER NOT NULL DEFAULT 0,
    step_name TEXT NOT NULL DEFAULT 'default',
    input_messages TEXT DEFAULT '[]',
    output_text TEXT DEFAULT '',
    model_id TEXT DEFAULT '',
    latency_ms REAL DEFAULT 0,
    token_usage TEXT DEFAULT '{}',
    cost_usd REAL DEFAULT 0,
    trace_id TEXT,
    span_id TEXT,
    metadata TEXT DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_steps_branch ON step_outputs(branch_id);

-- Comparisons (A vs B scoring results)
CREATE TABLE IF NOT EXISTS comparisons (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    branch_a_id TEXT NOT NULL REFERENCES branches(id),
    branch_b_id TEXT NOT NULL REFERENCES branches(id),
    eval_run_id TEXT,
    test_case_label TEXT DEFAULT '',
    divergence_score REAL,
    step_divergence_scores TEXT DEFAULT '{}',
    eval_results TEXT DEFAULT '{}',
    scoring_status TEXT DEFAULT 'pending',
    retry_count INTEGER DEFAULT 0,
    evaluator_configs TEXT DEFAULT '[]',
    review_status TEXT DEFAULT 'pending',
    assigned_to TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    scored_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_comp_run ON comparisons(run_id);
CREATE INDEX IF NOT EXISTS idx_comp_scoring ON comparisons(scoring_status);

-- Decisions (human judgments on comparisons)
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    comparison_id TEXT NOT NULL REFERENCES comparisons(id) ON DELETE CASCADE,
    eval_run_id TEXT,
    choice TEXT NOT NULL,
    confidence TEXT DEFAULT 'medium',
    rationale TEXT DEFAULT '',
    reviewer_id TEXT DEFAULT '',
    provenance_hash TEXT DEFAULT '',
    data_category TEXT DEFAULT '',
    metadata TEXT DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_decisions_comp ON decisions(comparison_id);

-- Test sets
CREATE TABLE IF NOT EXISTS test_sets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    version INTEGER DEFAULT 1,
    is_frozen BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Test cases
CREATE TABLE IF NOT EXISTS test_cases (
    id TEXT PRIMARY KEY,
    test_set_id TEXT NOT NULL REFERENCES test_sets(id) ON DELETE CASCADE,
    input_messages TEXT NOT NULL DEFAULT '[]',
    expected_output TEXT DEFAULT '',
    domain TEXT NOT NULL DEFAULT '',
    industry TEXT NOT NULL DEFAULT '',
    use_case_type TEXT NOT NULL DEFAULT '',
    failure_mode TEXT NOT NULL DEFAULT '',
    test_goal TEXT NOT NULL DEFAULT '',
    metadata TEXT DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cases_set ON test_cases(test_set_id);

-- Eval runs (batch evaluation executions)
CREATE TABLE IF NOT EXISTS eval_runs (
    id TEXT PRIMARY KEY,
    workflow_id TEXT REFERENCES workflows(id),
    test_set_id TEXT REFERENCES test_sets(id),
    governed_model_id TEXT,
    name TEXT DEFAULT '',
    status TEXT DEFAULT 'running',
    config TEXT DEFAULT '{}',
    results_summary TEXT DEFAULT '{}',
    started_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_eval_workflow ON eval_runs(workflow_id);

-- Model inventory for MRM governance
CREATE TABLE IF NOT EXISTS model_inventory (
    model_id              TEXT PRIMARY KEY,
    display_name          TEXT NOT NULL,
    provider              TEXT NOT NULL DEFAULT '',
    version               TEXT NOT NULL DEFAULT '',
    use_case              TEXT NOT NULL DEFAULT '',
    risk_tier             TEXT NOT NULL DEFAULT 'MEDIUM',
    regulatory_frameworks TEXT NOT NULL DEFAULT '[]',
    deployed_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_validated_at     TIMESTAMPTZ,
    next_validation_due   TIMESTAMPTZ,
    owner_team            TEXT NOT NULL DEFAULT '',
    documentation_url     TEXT NOT NULL DEFAULT '',
    status                TEXT NOT NULL DEFAULT 'ACTIVE',
    present_artifacts     TEXT NOT NULL DEFAULT '[]',
    evaluation_signals    JSONB NOT NULL DEFAULT '{}',
    created_at            TIMESTAMPTZ DEFAULT NOW(),
    updated_at            TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_model_inventory_status ON model_inventory(status);
CREATE INDEX IF NOT EXISTS idx_model_inventory_due ON model_inventory(next_validation_due);

-- Compliance reports history
CREATE TABLE IF NOT EXISTS compliance_reports (
    id                TEXT PRIMARY KEY,
    model_id          TEXT NOT NULL,
    framework         TEXT NOT NULL,
    generated_at      TIMESTAMPTZ DEFAULT NOW(),
    findings_count    INTEGER NOT NULL DEFAULT 0,
    coverage_complete INTEGER NOT NULL DEFAULT 0,
    format            TEXT NOT NULL DEFAULT 'json'
);
CREATE INDEX IF NOT EXISTS idx_compliance_reports_model ON compliance_reports(model_id);

-- Dedicated table for LLM providers
CREATE TABLE IF NOT EXISTS llm_providers (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    provider_type   TEXT NOT NULL DEFAULT 'openai',
    base_url        TEXT NOT NULL DEFAULT '',
    api_key_encrypted TEXT NOT NULL DEFAULT '',
    is_default      INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_providers_default ON llm_providers(is_default);

-- Test case performance tracking
CREATE TABLE IF NOT EXISTS test_case_performance (
    id               TEXT PRIMARY KEY,
    test_case_label  TEXT NOT NULL,
    workflow_id      TEXT NOT NULL,
    eval_run_id      TEXT NOT NULL,
    comparison_id    TEXT,
    divergence_score REAL,
    decision_choice  TEXT,
    reviewer_confidence TEXT,
    recorded_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_tc_perf_label ON test_case_performance(test_case_label, workflow_id);
CREATE INDEX IF NOT EXISTS idx_tc_perf_eval_run ON test_case_performance(eval_run_id);

-- Reviewer profiles
CREATE TABLE IF NOT EXISTS reviewer_profiles (
    reviewer_id      TEXT PRIMARY KEY,
    display_name     TEXT NOT NULL DEFAULT '',
    role             TEXT NOT NULL DEFAULT 'reviewer',
    expertise_level  TEXT NOT NULL DEFAULT 'intermediate',
    domain_expertise TEXT NOT NULL DEFAULT '[]',
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

-- Data consent
CREATE TABLE IF NOT EXISTS data_consent (
    id           TEXT PRIMARY KEY,
    scope        TEXT NOT NULL DEFAULT 'global',
    workflow_id  TEXT,
    consent_type TEXT NOT NULL,
    granted_by   TEXT NOT NULL,
    granted_at   TIMESTAMPTZ DEFAULT NOW(),
    expires_at   TIMESTAMPTZ,
    is_active    INTEGER NOT NULL DEFAULT 1,
    notes        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_consent_scope ON data_consent(scope, workflow_id, consent_type, is_active);

-- Comments
CREATE TABLE IF NOT EXISTS comments (
    id              TEXT PRIMARY KEY,
    comparison_id   TEXT NOT NULL REFERENCES comparisons(id) ON DELETE CASCADE,
    author_id       TEXT NOT NULL,
    author_name     TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL,
    parent_id       TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    is_resolved     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_comments_comparison ON comments(comparison_id, created_at);
CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(parent_id);

-- Review assignments
CREATE TABLE IF NOT EXISTS review_assignments (
    id              TEXT PRIMARY KEY,
    eval_run_id     TEXT NOT NULL,
    comparison_id   TEXT NOT NULL REFERENCES comparisons(id) ON DELETE CASCADE,
    reviewer_id     TEXT NOT NULL,
    assigned_by     TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    assigned_at     TIMESTAMPTZ DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    notes           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_assignments_reviewer ON review_assignments(reviewer_id, status);
CREATE INDEX IF NOT EXISTS idx_assignments_eval_run ON review_assignments(eval_run_id, status);
CREATE INDEX IF NOT EXISTS idx_assignments_comparison ON review_assignments(comparison_id);

-- Trace events (agent execution trees)
CREATE TABLE IF NOT EXISTS trace_events (
    id TEXT PRIMARY KEY,
    branch_id TEXT NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    parent_event_id TEXT REFERENCES trace_events(id),
    event_type TEXT NOT NULL DEFAULT 'tool_call',
    event_index INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL DEFAULT '',
    input_data JSONB DEFAULT '{}',
    output_data JSONB DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'completed',
    latency_ms INTEGER DEFAULT 0,
    tokens_input INTEGER DEFAULT 0,
    tokens_output INTEGER DEFAULT 0,
    cost_usd REAL,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_trace_events_branch ON trace_events(branch_id);
CREATE INDEX IF NOT EXISTS idx_trace_events_run ON trace_events(run_id);
CREATE INDEX IF NOT EXISTS idx_trace_events_parent ON trace_events(parent_event_id);

-- Trajectory outcomes (agent comparisons)
CREATE TABLE IF NOT EXISTS trajectory_outcomes (
    id TEXT PRIMARY KEY,
    comparison_id TEXT NOT NULL REFERENCES comparisons(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    workflow_id TEXT NOT NULL,
    tool_sequence_score REAL DEFAULT 0.0,
    outcome_equivalence_score REAL DEFAULT 0.0,
    efficiency_score REAL DEFAULT 0.0,
    trajectory_score REAL DEFAULT 0.0,
    tool_sequence_detail JSONB DEFAULT '{}',
    outcome_detail JSONB DEFAULT '{}',
    efficiency_detail JSONB DEFAULT '{}',
    branch_a_tool_count INTEGER DEFAULT 0,
    branch_b_tool_count INTEGER DEFAULT 0,
    branch_a_depth INTEGER DEFAULT 0,
    branch_b_depth INTEGER DEFAULT 0,
    branch_a_total_latency_ms INTEGER DEFAULT 0,
    branch_b_total_latency_ms INTEGER DEFAULT 0,
    branch_a_total_cost_usd REAL DEFAULT 0.0,
    branch_b_total_cost_usd REAL DEFAULT 0.0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_trajectory_outcomes_comparison ON trajectory_outcomes(comparison_id);
CREATE INDEX IF NOT EXISTS idx_trajectory_outcomes_workflow ON trajectory_outcomes(workflow_id);

-- Settings (per-workspace config like scorer preferences)
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
"""


# ---------------------------------------------------------------------------
# Control plane DDL (public schema — shared across all workspaces)
# ---------------------------------------------------------------------------

CONTROL_PLANE_DDL = """
-- Organizations
CREATE TABLE IF NOT EXISTS organizations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    domain TEXT NOT NULL UNIQUE,
    settings JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Workspaces registry
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES organizations(id),
    name TEXT NOT NULL,
    schema_name TEXT NOT NULL UNIQUE,
    is_sandbox BOOLEAN DEFAULT FALSE,
    created_by TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_ws_org ON workspaces(org_id);

-- Workspace memberships
CREATE TABLE IF NOT EXISTS workspace_memberships (
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    org_id TEXT NOT NULL REFERENCES organizations(id),
    role TEXT NOT NULL DEFAULT 'evaluator',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, workspace_id)
);
CREATE INDEX IF NOT EXISTS idx_wm_user ON workspace_memberships(user_id);
CREATE INDEX IF NOT EXISTS idx_wm_workspace ON workspace_memberships(workspace_id);

-- API keys (enhanced with workspace scoping)
CREATE TABLE IF NOT EXISTS api_keys_v2 (
    id TEXT PRIMARY KEY,
    key_hash TEXT NOT NULL,
    key_prefix TEXT NOT NULL,
    org_id TEXT NOT NULL REFERENCES organizations(id),
    workspace_id TEXT REFERENCES workspaces(id),
    scope TEXT DEFAULT 'sdk:write',
    label TEXT DEFAULT '',
    created_by TEXT DEFAULT '',
    expires_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_keys_org ON api_keys_v2(org_id);
CREATE INDEX IF NOT EXISTS idx_keys_prefix ON api_keys_v2(key_prefix);

-- Audit log
CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    org_id TEXT NOT NULL,
    workspace_id TEXT,
    actor_id TEXT NOT NULL,
    actor_type TEXT NOT NULL DEFAULT 'user',
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT DEFAULT '',
    metadata JSONB DEFAULT '{}',
    ip_address INET,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_org_time ON audit_log(org_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_workspace ON audit_log(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_id, created_at DESC);
"""


# ---------------------------------------------------------------------------
# Workspace Provisioner
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    """Convert workspace name to a safe schema-compatible slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = slug.strip("_")
    return slug[:50]  # PostgreSQL identifier limit is 63 chars, leave room for prefix


class WorkspaceProvisioner:
    """Creates, lists, and destroys workspace schemas.

    Thread-safe. Uses the admin (control plane) database connection for
    schema-level DDL operations.
    """

    def __init__(self, database_url: str):
        """Initialize with a PostgreSQL connection string for admin operations."""
        self._database_url = database_url
        self._adapter = None
        self._init_lock = threading.Lock()
        self._initialized = False

    def _get_adapter(self):
        """Lazy-init the PostgreSQL adapter for control plane operations."""
        if self._adapter is None:
            from core.store import _PostgreSQLConn
            self._adapter = _PostgreSQLConn(self._database_url)
        return self._adapter

    def initialize_control_plane(self):
        """Create control plane tables in public schema. Call once at startup."""
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            adapter = self._get_adapter()
            with adapter.connect() as conn:
                conn.execute("SET search_path TO public")
                # Execute DDL (need to handle the multi-statement case)
                for statement in CONTROL_PLANE_DDL.split(";"):
                    statement = statement.strip()
                    if statement:
                        try:
                            conn.execute(statement)
                        except Exception as e:
                            # IF NOT EXISTS handles most cases; log others
                            if "already exists" not in str(e).lower():
                                logger.warning("Control plane DDL statement warning: %s", e)
            self._initialized = True
            logger.info("Control plane tables initialized")

    def create_workspace(
        self,
        org_id: str,
        name: str,
        created_by: str,
        is_sandbox: bool = False,
    ) -> str:
        """Create a new workspace with its own PostgreSQL schema.

        Returns the workspace_id (slug).
        Raises ValueError if workspace already exists.
        """
        workspace_id = _slugify(name)
        schema_name = f"workspace_{workspace_id}"

        adapter = self._get_adapter()
        with adapter.connect() as conn:
            # Check if workspace already exists
            existing = conn.fetchone(
                ("SELECT id FROM workspaces WHERE id = ? AND org_id = ?"),
                (workspace_id, org_id),
            )
            if existing:
                raise ValueError(f"Workspace '{workspace_id}' already exists in org '{org_id}'")

            # Create the schema
            # NOTE: schema names can't be parameterized; validated via _slugify
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema_name}")

            # Apply workspace DDL inside the new schema
            conn.execute(f"SET search_path TO {schema_name}")
            for statement in WORKSPACE_SCHEMA_DDL.split(";"):
                statement = statement.strip()
                if statement:
                    try:
                        conn.execute(statement)
                    except Exception as e:
                        if "already exists" not in str(e).lower():
                            logger.warning("Workspace DDL warning for %s: %s", schema_name, e)

            # Register in control plane
            conn.execute("SET search_path TO public")
            conn.execute(
                (
                    "INSERT INTO workspaces (id, org_id, name, schema_name, is_sandbox, created_by) "
                    "VALUES (?, ?, ?, ?, ?, ?)"
                ),
                (workspace_id, org_id, name, schema_name, is_sandbox, created_by),
            )

        logger.info("Created workspace '%s' (schema: %s) for org %s", name, schema_name, org_id)
        self._audit(org_id, workspace_id, created_by, "workspace.created", "workspace", workspace_id)
        return workspace_id

    def delete_workspace(self, org_id: str, workspace_id: str, deleted_by: str):
        """Soft-delete: rename schema, mark as deleted. Data retained for recovery."""
        schema_name = f"workspace_{workspace_id}"
        tombstone = f"_deleted_{workspace_id}_{int(time.time())}"

        adapter = self._get_adapter()
        with adapter.connect() as conn:
            conn.execute("SET search_path TO public")

            # Verify ownership
            row = conn.fetchone(
                ("SELECT org_id FROM workspaces WHERE id = ? AND org_id = ?"),
                (workspace_id, org_id),
            )
            if not row:
                raise ValueError(f"Workspace '{workspace_id}' not found in org '{org_id}'")

            # Rename schema (soft delete — data preserved)
            conn.execute(f"ALTER SCHEMA {schema_name} RENAME TO {tombstone}")

            # Mark deleted in control plane
            conn.execute(
                ("UPDATE workspaces SET deleted_at = NOW() WHERE id = ? AND org_id = ?"),
                (workspace_id, org_id),
            )

        logger.info("Deleted workspace '%s' (tombstone: %s)", workspace_id, tombstone)
        self._audit(org_id, workspace_id, deleted_by, "workspace.deleted", "workspace", workspace_id)

    def list_workspaces(self, org_id: str) -> List[Workspace]:
        """List all active workspaces for an organization."""
        adapter = self._get_adapter()
        with adapter.connect() as conn:
            conn.execute("SET search_path TO public")
            rows = conn.fetchall(
                (
                    "SELECT id, org_id, name, schema_name, is_sandbox, created_by, "
                    "created_at, deleted_at FROM workspaces "
                    "WHERE org_id = ? AND deleted_at IS NULL ORDER BY created_at"
                ),
                (org_id,),
            )
        return [
            Workspace(
                id=dict(r)["id"],
                org_id=dict(r)["org_id"],
                name=dict(r)["name"],
                schema_name=dict(r)["schema_name"],
                is_sandbox=dict(r).get("is_sandbox", False),
                created_by=dict(r).get("created_by", ""),
                created_at=str(dict(r).get("created_at", "")),
                deleted_at=None,
            )
            for r in rows
        ]

    def add_member(
        self, org_id: str, workspace_id: str, user_id: str, role: str = "evaluator"
    ):
        """Add a user to a workspace with a given role."""
        adapter = self._get_adapter()
        with adapter.connect() as conn:
            conn.execute("SET search_path TO public")
            conn.execute(
                (
                    "INSERT INTO workspace_memberships (user_id, workspace_id, org_id, role) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (user_id, workspace_id) DO UPDATE SET role = EXCLUDED.role"
                ),
                (user_id, workspace_id, org_id, role),
            )
        self._audit(org_id, workspace_id, user_id, "workspace.member_added", "membership", user_id)

    def remove_member(self, org_id: str, workspace_id: str, user_id: str):
        """Remove a user from a workspace."""
        adapter = self._get_adapter()
        with adapter.connect() as conn:
            conn.execute("SET search_path TO public")
            conn.execute(
                (
                    "DELETE FROM workspace_memberships WHERE user_id = ? AND workspace_id = ? AND org_id = ?"
                ),
                (user_id, workspace_id, org_id),
            )
        self._audit(org_id, workspace_id, user_id, "workspace.member_removed", "membership", user_id)

    def get_user_workspaces(self, user_id: str) -> List[Dict[str, Any]]:
        """Get all workspace memberships for a user."""
        adapter = self._get_adapter()
        with adapter.connect() as conn:
            conn.execute("SET search_path TO public")
            rows = conn.fetchall(
                (
                    "SELECT wm.workspace_id, wm.role, wm.org_id, w.name, w.is_sandbox "
                    "FROM workspace_memberships wm "
                    "JOIN workspaces w ON w.id = wm.workspace_id "
                    "WHERE wm.user_id = ? AND w.deleted_at IS NULL"
                ),
                (user_id,),
            )
        return [dict(r) for r in rows]

    def get_user_role(self, user_id: str, workspace_id: str) -> Optional[str]:
        """Get a user's role in a specific workspace. Returns None if not a member."""
        adapter = self._get_adapter()
        with adapter.connect() as conn:
            conn.execute("SET search_path TO public")
            row = conn.fetchone(
                (
                    "SELECT role FROM workspace_memberships WHERE user_id = ? AND workspace_id = ?"
                ),
                (user_id, workspace_id),
            )
        if row:
            return dict(row)["role"]
        return None

    def create_organization(self, org_id: str, name: str, domain: str) -> Organization:
        """Create a new organization in the control plane."""
        adapter = self._get_adapter()
        with adapter.connect() as conn:
            conn.execute("SET search_path TO public")
            conn.execute(
                (
                    "INSERT INTO organizations (id, name, domain) VALUES (?, ?, ?) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                (org_id, name, domain),
            )
        return Organization(id=org_id, name=name, domain=domain)

    def _audit(
        self, org_id: str, workspace_id: str, actor_id: str,
        action: str, resource_type: str, resource_id: str,
    ):
        """Write an audit log entry. Best-effort (never throws)."""
        try:
            adapter = self._get_adapter()
            with adapter.connect() as conn:
                conn.execute("SET search_path TO public")
                conn.execute(
                    (
                        "INSERT INTO audit_log (org_id, workspace_id, actor_id, actor_type, "
                        "action, resource_type, resource_id) VALUES (?, ?, ?, 'system', ?, ?, ?)"
                    ),
                    (org_id, workspace_id, actor_id, action, resource_type, resource_id),
                )
        except Exception as e:
            logger.warning("Audit log write failed: %s", e)


# ---------------------------------------------------------------------------
# Workspace Context (injected into request handlers)
# ---------------------------------------------------------------------------

@dataclass
class WorkspaceContext:
    """Request-scoped context carrying workspace identity and scoped DB access."""
    workspace_id: str
    org_id: str
    schema_name: str
    role: str
    user_id: str = ""
    is_sandbox: bool = False

    @contextmanager
    def connect(self, adapter):
        """Get a database connection scoped to this workspace's schema."""
        with adapter.connect() as conn:
            # Bind connection to workspace schema — prevents cross-workspace queries
            conn.execute(f"SET search_path TO {self.schema_name}, public")
            yield conn
            # Reset is handled by the adapter's connection pool return
