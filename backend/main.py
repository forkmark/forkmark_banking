"""ForkMark FastAPI backend — modular router architecture.

This file is the application entry point. All route logic lives in
backend/routes/ modules. Shared dependencies (db, auth, caching) are
in backend/deps.py.

v0.1.1 refactor: split from monolithic 2200-line main.py into:
  - deps.py          — db, redis, auth, rate limiting, caching
  - routes/sdk.py    — SDK endpoints (always require API key)
  - routes/eval_runs.py  — eval run CRUD + stats
  - routes/test_sets.py  — test set management
  - routes/workflows.py  — workflow CRUD + runs
  - routes/comparisons.py — comparisons, decisions, costs
  - routes/decisions.py  — decision listing + human-review audit exports (JSONL/CSV)
  - routes/keys.py       — API key management
  - routes/settings.py   — settings, system info, reviewer profiles, consent
  - routes/collaboration.py — comments + review assignments
  - routes/exports.py   — preference corpus export
  - routes/stats.py     — dashboard stats + charts
  - routes/runner.py    — no-code runner + playground
  - routes/demos.py     — demo seeding
  - routes/admin.py     — pruning + maintenance
  - routes/health.py    — liveness/readiness probes
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.middleware.base import BaseHTTPMiddleware

from config import config
from backend.deps import db


# ── Lifespan ─────────────────────────────────────────────────────────────────

async def _refresh_pricing_table():
    """Refresh the LLM price table from LiteLLM upstream, then cache it to disk.
    Runs detached from startup so a slow/absent network never delays boot."""
    import httpx
    try:
        url = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()

        new_prices = {}
        for model, info in data.items():
            if isinstance(info, dict) and "input_cost_per_token" in info and "output_cost_per_token" in info:
                try:
                    new_prices[model] = {
                        "input": float(info["input_cost_per_token"]) * 1_000_000,
                        "output": float(info["output_cost_per_token"]) * 1_000_000,
                    }
                except (ValueError, TypeError):
                    pass
        if new_prices:
            from core.store import update_pricing_table, save_pricing_cache
            update_pricing_table(new_prices)
            save_pricing_cache()   # persist merged table for offline restarts
    except Exception as e:
        print(f"Warning: LLM price refresh failed (using cached/bundled prices): {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: load cached prices instantly, refresh upstream in the background,
    and recover any scoring interrupted by a previous crash/restart."""
    import asyncio

    # 1. Load last-known prices from disk — instant and offline-friendly. Falls
    #    back to the bundled _DEFAULT_PRICES table when no cache exists.
    try:
        from core.store import load_cached_pricing
        load_cached_pricing()
    except Exception as e:
        print(f"Warning: could not load cached pricing: {e}")

    # 2. Refresh from LiteLLM upstream in the background — never blocks startup.
    asyncio.create_task(_refresh_pricing_table())

    # Recover scoring interrupted by a previous crash/restart. The in-process
    # background queue is not durable, so re-enqueue any comparisons still
    # marked 'pending'/'running' from a prior run. Detached so startup is fast.
    try:
        from core.background import recover_pending_scoring
        n = await recover_pending_scoring(db)
        if n:
            print(f"[forkmark] Re-enqueued {n} interrupted comparison(s) for scoring.")
    except Exception as e:
        print(f"Warning: scoring recovery sweep failed: {e}")
    yield


# ── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="ForkMark",
    version=config.VERSION,
    description=(
        "Self-hosted LLM model risk management and validation platform for "
        "regulated financial institutions: model inventory, defensible A/B "
        "statistics, bias and numerical-fidelity evaluators, human-review "
        "capture, regulatory coverage tracking, and validation memoranda."
    ),
    lifespan=lifespan,
)

# Auth is header-based (X-API-Key), never cookie-based, so credentialed CORS is
# not needed — disabling it removes the cookie/CSRF surface. The localhost regex
# stays for the Vite dev server; production is served same-origin by FastAPI.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_origin_regex=r"http://localhost:\d+",
    allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
)


# ── Enterprise stack (conditional) ───────────────────────────────────────────

def _init_enterprise_stack():
    """Initialize enterprise modules when FM_ENTERPRISE_MODE is enabled.

    The OSS edition ships with the model-risk core plus role-based access control
    and an immutable audit log (see backend/deps.py and core/store_impl/audit.py).
    The heavier enterprise capabilities — multi-tenancy, SCIM/SSO provisioning,
    device-flow auth, data-residency routing, the Redis message bus, and OTel
    observability — live in the `ee/` package and some require extra services
    (PostgreSQL, Redis, WorkOS). They are loaded opt-in via FM_ENTERPRISE_MODE.

    Each stage is wrapped in its own try/except so a missing optional dependency
    degrades gracefully to OSS behaviour instead of breaking startup. This makes
    the enterprise stack genuinely loadable and testable, without forcing those
    dependencies on a default self-host.
    """
    enterprise_mode = os.environ.get("FM_ENTERPRISE_MODE", "").lower() in ("true", "1", "yes")
    if not enterprise_mode:
        print("[forkmark] OSS edition — RBAC + audit log active; ee/ stack not loaded "
              "(set FM_ENTERPRISE_MODE=true to enable).")
        return

    print("[forkmark] FM_ENTERPRISE_MODE=true — attempting to load ee/ stack.")
    app.state.db = db
    multi_tenant = os.environ.get("FM_MULTI_TENANT", "").lower() in ("true", "1", "yes")
    database_url = getattr(config, "DATABASE_URL", None)
    redis_url = getattr(config, "REDIS_URL", None)

    def _stage(name, fn):
        try:
            fn()
            print(f"[forkmark]   ✓ enterprise: {name}")
        except Exception as e:  # pragma: no cover - depends on optional services
            print(f"[forkmark]   ✗ enterprise: {name} unavailable — {e}. "
                  f"Continuing without it.")

    def _workspaces():
        from ee.workspace_router import get_workspace_router
        app.state.workspace_router = get_workspace_router(
            database_url, str(getattr(config, "DB_PATH", "")), multi_tenant)

    def _message_bus():
        from ee.message_bus import get_message_bus
        app.state.message_bus = get_message_bus(redis_url)

    def _audit_bridge():
        # Bridge the OSS audit store into the ee AuditLogger interface if present.
        from ee.audit import AuditLogger
        app.state.audit_logger = AuditLogger(getattr(app.state, "workspace_router", None))

    def _scim():
        if not multi_tenant:
            return
        from ee.multitenancy import WorkspaceProvisioner
        from ee.scim_handler import SCIMProvisioner, scim_router
        provisioner = WorkspaceProvisioner(database_url or "")
        secret = os.environ.get("WORKOS_WEBHOOK_SECRET", "")
        app.state.scim_provisioner = SCIMProvisioner(provisioner, secret)
        app.include_router(scim_router, prefix="/api/webhooks")

    for name, fn in (
        ("workspace_router", _workspaces),
        ("message_bus", _message_bus),
        ("audit_logger", _audit_bridge),
        ("scim_provisioning", _scim),
    ):
        _stage(name, fn)

_init_enterprise_stack()


# ── Register route modules ───────────────────────────────────────────────────

from backend.routes.sdk import router as sdk_router
from backend.routes.eval_runs import router as eval_runs_router
from backend.routes.test_sets import router as test_sets_router
from backend.routes.workflows import router as workflows_router
from backend.routes.comparisons import router as comparisons_router
from backend.routes.decisions import router as decisions_router
from backend.routes.keys import router as keys_router
from backend.routes.settings import router as settings_router
from backend.routes.collaboration import router as collaboration_router
from backend.routes.runner import router as runner_router
from backend.routes.demos import router as demos_router
from backend.routes.providers import router as providers_router
from backend.routes.admin import router as admin_router
from backend.routes.health import router as health_router
from backend.routes.regulatory import router as regulatory_router
from backend.routes.inventory import router as inventory_router
from backend.routes.statistics import router as statistics_router
from backend.routes.compliance import router as compliance_router
from backend.routes.audit import router as audit_router

# Agent comparison router (feature-gated)
if config.ENABLE_AGENT_COMPARISON:
    from backend.routes.agent import router as agent_router

app.include_router(sdk_router)
app.include_router(eval_runs_router)
app.include_router(test_sets_router)
app.include_router(workflows_router)
app.include_router(comparisons_router)
app.include_router(decisions_router)
app.include_router(keys_router)
app.include_router(settings_router)
app.include_router(collaboration_router)
app.include_router(runner_router)
app.include_router(providers_router)
app.include_router(demos_router)
app.include_router(admin_router)
app.include_router(health_router)
app.include_router(regulatory_router)
app.include_router(inventory_router)
app.include_router(statistics_router)
app.include_router(compliance_router)
app.include_router(audit_router)
if config.ENABLE_AGENT_COMPARISON:
    app.include_router(agent_router)


# ── API v1 prefix alias ─────────────────────────────────────────────────────

class _V1RewriteMiddleware(BaseHTTPMiddleware):
    """Transparently strip /api/v1 → /api so both prefixes work."""
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/api/v1/"):
            scope = request.scope
            scope["path"] = "/api/" + request.url.path[len("/api/v1/"):]
            scope["raw_path"] = scope["path"].encode("ascii")
        return await call_next(request)

app.add_middleware(_V1RewriteMiddleware)


# ── Serve React SPA ──────────────────────────────────────────────────────────

_DIST = Path(__file__).parent.parent / "frontend" / "dist"
if _DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(_DIST / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(404, f"API route not found: /{full_path}")
        return FileResponse(str(_DIST / "index.html"))
