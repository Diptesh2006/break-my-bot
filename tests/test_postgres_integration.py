"""Opt-in live PostgreSQL contract tests (set BREAKMYBOT_TEST_POSTGRES_URL)."""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from breakmybot import store
from breakmybot.models import RunRecord


POSTGRES_URL = os.getenv("BREAKMYBOT_TEST_POSTGRES_URL", "").strip()
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="live PostgreSQL URL not configured")


def test_postgres_storage_migrations_events_and_concurrent_worker_claims(monkeypatch):
    monkeypatch.setenv("BREAKMYBOT_DATABASE_URL", POSTGRES_URL)
    suffix = uuid4().hex[:12]
    records = [
        RunRecord(id=f"pg-run-{suffix}-{index}", target_url="http://localhost:8000",
                  endpoint_path="/chat", method="GET", profile="quick")
        for index in range(3)
    ]
    saved_run_ids: list[str] = []
    try:
        for record in records:
            store.save(record)
            saved_run_ids.append(record.id)
            store.enqueue(record.id, {"endpoint_path": "/chat", "method": "GET"})

        with store.connect() as db:
            versions = db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        assert [row[0] for row in versions] == [1, 2]
        assert store.get(records[0].id) == records[0]

        sequence = store.append_event(records[0].id, {"type": "integration.test"})
        assert store.events_after(records[0].id, sequence - 1) == [
            (sequence, {"type": "integration.test"})
        ]

        with ThreadPoolExecutor(max_workers=2) as executor:
            claimed = list(executor.map(lambda _: store.claim_job(), range(2)))
        assert all(item is not None for item in claimed)
        assert len({item[0] for item in claimed if item}) == 2
        for run_id, _request in claimed:
            store.acknowledge_job(run_id)

        assert store.cancel_job(records[2].id)
        assert store.job_cancel_requested(records[2].id)
        assert store.get(records[2].id).status == "CANCELLED"
        with store.connect() as db:
            assert db.execute("SELECT request FROM jobs WHERE run_id=?", (records[2].id,)).fetchone()[0] == "{}"
    finally:
        if saved_run_ids:
            test_failed = sys.exc_info()[0] is not None
            try:
                with store.connect() as db:
                    for run_id in saved_run_ids:
                        db.execute("DELETE FROM events WHERE run_id=?", (run_id,))
                        db.execute("DELETE FROM jobs WHERE run_id=?", (run_id,))
                        db.execute("DELETE FROM runs WHERE id=?", (run_id,))
            except Exception:
                if not test_failed:
                    raise
