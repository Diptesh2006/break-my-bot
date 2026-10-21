"""SQLite-leased background worker for durable local assessment execution."""

from __future__ import annotations

import argparse
import asyncio
import os

from . import runner, store
from .models import RunRequest
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path.cwd() / ".env")


async def run_worker(poll_seconds: float = 0.5) -> None:
    store.requeue_stale_jobs()
    store.recover_queued_runs()
    while True:
        claimed = store.claim_job()
        if claimed is None:
            await asyncio.sleep(poll_seconds)
            continue
        run_id, payload = claimed
        store.recover_queued_runs()
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
            # A hard/unexpected runner exception remains leased; after the worker exits,
            # another worker can reclaim it when the lease expires.
            raise
        finally:
            monitor.cancel()
        record = store.get(run_id)
        if record and record.status in {"COMPLETED", "FAILED", "CANCELLED", "PARTIAL"}:
            store.acknowledge_job(run_id)
        else:
            # The request process is no longer executing this job. Let the lease expire
            # instead of recording a false completion; recovery examines the run payload.
            with store.connect() as db:
                db.execute("UPDATE jobs SET lease_until=0 WHERE run_id=?", (run_id,))


def main() -> None:
    parser = argparse.ArgumentParser(description="BreakMyBot durable local worker")
    parser.add_argument("--poll-seconds", type=float, default=float(os.getenv("BREAKMYBOT_WORKER_POLL_SECONDS", "0.5")))
    args = parser.parse_args()
    asyncio.run(run_worker(max(0.1, args.poll_seconds)))


if __name__ == "__main__":
    main()
