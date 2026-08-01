"""Forkmark Enterprise Edition (``ee``) — dormant in the open-source build.

This package holds the enterprise/scaling modules that are **not loaded** by the
open-source Forkmark build (v0.1.x):

    auth_middleware   JWT / device-flow / workspace-scoped auth
    workspace_router  multi-tenant connection routing (per-workspace schemas)
    multitenancy      workspace provisioning + tenant context
    scim_handler      SCIM 2.0 user/group provisioning (WorkOS, etc.)
    device_flow       OAuth device-flow login for CLI/headless
    data_residency    region-pinned database/redis routing
    message_bus       Redis pub/sub event bus
    audit             structured audit logging
    celery_app        distributed background workers

Nothing in ``backend/`` or ``core/`` imports from ``ee`` at runtime — the OSS
app runs as a single FastAPI process with SQLite/PostgreSQL, a single shared API
key, and in-process background scoring. These modules are kept here as the basis
for a future, license-gated enterprise edition; see ``backend/main.py``'s
``_init_enterprise_stack`` for the (disabled) wiring.

To keep ``import ee`` cheap and dependency-free, this ``__init__`` intentionally
does **not** import the submodules (some of them require extras like ``celery``).
Import the specific module you need explicitly, e.g. ``from ee import multitenancy``.
"""

__all__ = [
    "auth_middleware",
    "workspace_router",
    "multitenancy",
    "scim_handler",
    "device_flow",
    "data_residency",
    "message_bus",
    "audit",
    "celery_app",
]
