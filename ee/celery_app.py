import os
import asyncio
import logging
from celery import Celery
from celery.schedules import crontab

logger = logging.getLogger("forkpoint.celery")

redis_url = os.getenv("FP_REDIS_URL", "redis://localhost:6379/0")

# Use Redis as both broker and result backend
celery_app = Celery(
    "forkpoint_tasks",
    broker=redis_url,
    backend=redis_url
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,                  # re-queue on worker crash
    worker_prefetch_multiplier=1,          # fair scheduling across workers
    task_reject_on_worker_lost=True,       # don't lose tasks on OOM kill
    worker_max_tasks_per_child=200,        # recycle workers to prevent memory leaks
    # Beat schedule for periodic maintenance tasks
    beat_schedule={
        "retry-failed-scoring": {
            "task": "retry_failed_scoring",
            "schedule": crontab(minute="*/5"),  # every 5 minutes
        },
        "cleanup-expired-rate-limits": {
            "task": "cleanup_rate_limits",
            "schedule": crontab(minute="*/10"),  # every 10 minutes
        },
    },
)


# ── Scoring task ─────────────────────────────────────────────────────────────

@celery_app.task(
    name="score_comparison",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
)
def run_scoring_task(self, comp_id: str, branch_a_id: str, branch_b_id: str, evaluator_configs: list):
    """
    Celery task for divergence scoring and evaluators.
    Runs durably in a worker process with automatic retry on failure.
    """
    from core.store import Database
    from config import config
    from core.background import _score_comparison_async

    try:
        # Instantiate the DB connection for this worker process
        db = Database(str(config.DB_PATH), database_url=config.DATABASE_URL)

        # Run the async core logic synchronously inside the celery worker
        asyncio.run(
            _score_comparison_async(db, comp_id, branch_a_id, branch_b_id, evaluator_configs)
        )
    except Exception as exc:
        logger.error("Scoring task failed for %s: %s", comp_id, exc)
        raise self.retry(exc=exc)


# ── Periodic maintenance tasks ───────────────────────────────────────────────

@celery_app.task(name="retry_failed_scoring")
def retry_failed_scoring():
    """Find comparisons stuck in 'failed' status and re-queue them (max 3 retries)."""
    from core.store import Database
    from config import config

    db = Database(str(config.DB_PATH), database_url=config.DATABASE_URL)
    failed = db.get_comparisons_by_scoring_status("failed", limit=50)

    requeued = 0
    for comp in failed:
        retry_count = comp.get("retry_count", 0)
        if retry_count < 3:
            db.update_comparison_scoring(
                comp["id"], scoring_status="pending", retry_count=retry_count + 1
            )
            run_scoring_task.delay(
                comp["id"], comp["branch_a_id"], comp["branch_b_id"],
                comp.get("evaluator_configs", []),
            )
            requeued += 1

    if requeued:
        logger.info("Re-queued %d failed scoring tasks", requeued)


@celery_app.task(name="cleanup_rate_limits")
def cleanup_rate_limits():
    """Clean up expired rate limit keys in Redis."""
    import redis as _redis
    try:
        r = _redis.from_url(redis_url)
        # Rate limit keys use pattern rate:*
        cursor = 0
        cleaned = 0
        while True:
            cursor, keys = r.scan(cursor, match="rate:*", count=100)
            for key in keys:
                # Remove entries older than the window
                import time
                r.zremrangebyscore(key, 0, time.time() - 60)
                # Delete key if empty
                if r.zcard(key) == 0:
                    r.delete(key)
                    cleaned += 1
            if cursor == 0:
                break
        if cleaned:
            logger.info("Cleaned %d expired rate limit keys", cleaned)
    except Exception as e:
        logger.warning("Rate limit cleanup failed: %s", e)
