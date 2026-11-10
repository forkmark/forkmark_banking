#!/usr/bin/env python3
"""ForkMark Continuous Drift Monitoring CLI Runner.

Executes all monitors whose next_run_at has arrived or passed using atomic
distributed leases to ensure zero duplicate runs across multiple nodes.

Usage:
    python -m scripts.run_monitors
    python scripts/run_monitors.py --db /path/to/db.sqlite
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.store import Database
from core.monitors.engine import execute_monitor_run

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("forkmark.scripts.run_monitors")


async def run_all_due_monitors(db: Database) -> int:
    """Run all monitors currently due. Returns number of executed monitors."""
    worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    now_iso = datetime.now(timezone.utc).isoformat()
    due_monitors = db.list_due_monitors(now_iso=now_iso)

    if not due_monitors:
        logger.info("No monitors are due to run at %s.", now_iso)
        return 0

    logger.info("Found %d due monitor(s). Attempting lease acquisition...", len(due_monitors))
    executed_count = 0

    for m in due_monitors:
        mid = m["id"]
        acquired = db.acquire_monitor_lease(mid, lease_owner=worker_id, lease_duration_seconds=1800)
        if not acquired:
            logger.info("Monitor %s claimed by another runner; skipping.", mid)
            continue

        logger.info("Acquired lease for monitor %s (model: %s). Running evaluation...", mid, m["governed_model_id"])
        try:
            res = await execute_monitor_run(
                db=db,
                monitor_id=mid,
                pre_acquired_lease=True,
                lease_owner=worker_id,
            )
            logger.info("Completed monitor %s: state=%s, status=%s", mid, res.get("state"), res.get("status"))
            executed_count += 1
        except Exception as exc:
            logger.exception("Failed executing monitor %s: %s", mid, exc)

    return executed_count


def main():
    parser = argparse.ArgumentParser(description="Run ForkMark continuous drift monitors.")
    parser.add_argument("--db", default=os.getenv("FM_DATABASE_PATH", "forkmark.db"), help="Path to SQLite database")
    args = parser.parse_args()

    db_url = os.getenv("FM_DATABASE_URL", "")
    db = Database(args.db, database_url=db_url)

    count = asyncio.run(run_all_due_monitors(db))
    logger.info("Drift monitoring cycle finished. Processed %d monitor(s).", count)


if __name__ == "__main__":
    main()

