"""Health, readiness, and ops endpoints."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import PlainTextResponse

from backend.response_models import HealthResponse
from backend.deps import db, redis_client, ui_read_auth
from config import config

router = APIRouter(tags=["health"])


@router.get("/healthz", include_in_schema=False)
def health_check():
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
def readiness_check():
    checks = {"database": "unknown", "redis": "unknown", "pgbouncer": "unknown"}
    db_url = os.getenv("FM_DATABASE_URL", "")
    is_pgbouncer = "pgbouncer" in db_url.lower() or ":6432" in db_url
    try:
        with db.adapter.connect() as conn:
            conn.execute("SELECT 1")
        checks["database"] = "connected"
        checks["pgbouncer"] = "connected (transaction mode)" if is_pgbouncer else "direct connection (no pooler)"
    except Exception as e:
        raise HTTPException(503, detail={"status": "not_ready", "checks": checks, "error": str(e)})

    if redis_client:
        try:
            redis_client.ping()
            checks["redis"] = "connected"
        except Exception:
            checks["redis"] = "disconnected (degraded mode)"
    else:
        checks["redis"] = "not configured"

    return {"status": "ready", "checks": checks}


@router.get("/health", include_in_schema=False)
@router.get("/api/health", response_model=HealthResponse)
def health():
    return {"status": "ok", "version": config.VERSION}


@router.get("/api/pool-stats", tags=["ops"])
def pool_stats(_auth=Depends(ui_read_auth)):
    pool_info = {
        "database_url_masked": "***pgbouncer***" if "pgbouncer" in os.getenv("FM_DATABASE_URL", "").lower()
                               else "***direct***",
        "pool_min": int(os.getenv("DB_POOL_MIN", "2")),
        "pool_max": int(os.getenv("DB_POOL_MAX", "10")),
    }
    try:
        adapter = db.adapter
        if hasattr(adapter, 'pool') and adapter.pool:
            pool = adapter.pool
            pool_info["pool_size"] = getattr(pool, 'size', None)
            pool_info["pool_checkedin"] = getattr(pool, 'checkedin', lambda: None)()
            pool_info["pool_checkedout"] = getattr(pool, 'checkedout', lambda: None)()
            pool_info["pool_overflow"] = getattr(pool, 'overflow', lambda: None)()
    except Exception:
        pass
    return pool_info


@router.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
def prometheus_metrics():
    """Prometheus text exposition format endpoint for bank observability platforms."""
    lines = [
        "# HELP forkmark_up Status of ForkMark banking service (1 = up)",
        "# TYPE forkmark_up gauge",
        "forkmark_up 1",
    ]
    try:
        from core.model_inventory import ModelInventory
        inv = ModelInventory(db).list_models()
        tier_counts = {"LOW": 0, "MEDIUM": 0, "HIGH": 0, "CRITICAL": 0}
        due_count = 0
        now_utc = datetime.now(timezone.utc)
        for m in inv:
            tier = getattr(m, "risk_tier", "MEDIUM") or "MEDIUM"
            tier_str = tier.value if hasattr(tier, "value") else str(tier)
            tier_counts[tier_str] = tier_counts.get(tier_str, 0) + 1
            due_at = getattr(m, "next_validation_due", None)
            if due_at:
                if isinstance(due_at, str):
                    try:
                        due_at_dt = datetime.fromisoformat(due_at)
                    except Exception:
                        due_at_dt = None
                else:
                    due_at_dt = due_at

                if due_at_dt:
                    if due_at_dt.tzinfo is None:
                        due_at_dt = due_at_dt.replace(tzinfo=timezone.utc)
                    if due_at_dt <= now_utc:
                        due_count += 1

        lines.append("# HELP forkmark_models_total Governed models registered in MRM inventory")
        lines.append("# TYPE forkmark_models_total gauge")
        for tier, count in tier_counts.items():
            lines.append(f'forkmark_models_total{{risk_tier="{tier}"}} {count}')

        lines.append("# HELP forkmark_models_revalidation_due Models currently overdue for revalidation")
        lines.append("# TYPE forkmark_models_revalidation_due gauge")
        lines.append(f"forkmark_models_revalidation_due {due_count}")
    except Exception as e:
        lines.append(f"# Error collecting inventory metrics: {e}")

    try:
        with db.adapter.connect() as conn:
            row = conn.execute("SELECT COUNT(*), COALESCE(MAX(seq), 0) FROM audit_log").fetchone()
            if row:
                total_audit, max_seq = row[0], row[1]
                lines.append("# HELP forkmark_audit_log_entries_total Cumulative audit log records")
                lines.append("# TYPE forkmark_audit_log_entries_total counter")
                lines.append(f"forkmark_audit_log_entries_total {total_audit}")
                lines.append("# HELP forkmark_audit_log_max_sequence Latest verified monotonic audit log sequence")
                lines.append("# TYPE forkmark_audit_log_max_sequence counter")
                lines.append(f"forkmark_audit_log_max_sequence {max_seq}")
    except Exception as e:
        lines.append(f"# Error collecting audit metrics: {e}")

    try:
        with db.adapter.connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) FROM eval_runs GROUP BY status").fetchall()
            lines.append("# HELP forkmark_eval_runs_total Total evaluation runs by status")
            lines.append("# TYPE forkmark_eval_runs_total counter")
            for r in rows:
                lines.append(f'forkmark_eval_runs_total{{status="{r[0]}"}} {r[1]}')
    except Exception as e:
        lines.append(f"# Error collecting eval runs metrics: {e}")

    return PlainTextResponse("\n".join(lines) + "\n")

