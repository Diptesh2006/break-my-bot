"""Database-leased background worker for durable local assessment execution."""

from __future__ import annotations

import argparse
import asyncio
import os

from . import runner, store
from .models import RunRequest
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path.cwd() / ".env")


async def process_one_job() -> bool:
    store.expire_stale_jobs()
    claimed = store.claim_job()
    if claimed is None:
        return False
    run_id, payload = claimed
    cancel_event = asyncio.Event()

    async def monitor_cancel() -> None:
        while not cancel_event.is_set():
            if store.job_cancel_requested(run_id):
                cancel_event.set()
                return
            store.renew_job_lease(run_id)
            await asyncio.sleep(5)

    monitor = asyncio.create_task(monitor_cancel())
    try:
        await runner.execute(run_id, RunRequest.model_validate(payload), cancel_event)
    except Exception:
        from datetime import datetime, timezone
        record = store.get(run_id)
        if record and record.status in {"CREATED", "RUNNING"}:
            record.status = "FAILED"
            record.error = "Worker failed unexpectedly; automatic replay was suppressed to avoid duplicate target side effects."
            record.completed_at = datetime.now(timezone.utc)
            store.save(record)
            store.append_event(run_id, {"type": "run.failed", "phase": "FAILED", "reason": "worker_exception"})
    finally:
        monitor.cancel()
    record = store.get(run_id)
    if record and record.status in {"COMPLETED", "FAILED", "CANCELLED", "PARTIAL"}:
        store.acknowledge_job(run_id)
    else:
        store.expire_stale_jobs()
    return True


async def run_worker(poll_seconds: float = 0.5) -> None:
    while True:
        if not await process_one_job():
            await asyncio.sleep(poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="BreakMyBot durable local worker")
    parser.add_argument("--poll-seconds", type=float, default=float(os.getenv("BREAKMYBOT_WORKER_POLL_SECONDS", "0.5")))
    args = parser.parse_args()
    asyncio.run(run_worker(max(0.1, args.poll_seconds)))


if __name__ == "__main__":
    main()
