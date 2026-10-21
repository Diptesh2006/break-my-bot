from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from .models import RunRecord


def data_dir() -> Path:
    path = Path(os.getenv("BREAKMYBOT_DATA_DIR", "./data")).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(data_dir() / "breakmybot.sqlite3", timeout=10)
    db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, event TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS jobs (run_id TEXT PRIMARY KEY, request TEXT NOT NULL, status TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)")
    return db


def save(run: RunRecord) -> None:
    payload = json.dumps(run.model_dump(mode="json"), ensure_ascii=False)
    with connect() as db:
        db.execute("INSERT INTO runs(id,created_at,payload) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                   (run.id, run.created_at.isoformat(), payload))


def get(run_id: str) -> RunRecord | None:
    with connect() as db:
        row = db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
    return RunRecord.model_validate_json(row[0]) if row else None


def latest(limit: int = 50) -> list[RunRecord]:
    with connect() as db:
        rows = db.execute("SELECT payload FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [RunRecord.model_validate_json(row[0]) for row in rows]


def append_event(run_id: str, event: dict) -> int:
    with connect() as db:
        cursor = db.execute("INSERT INTO events(run_id,event) VALUES(?,?)", (run_id, json.dumps(event)))
        return int(cursor.lastrowid)


def events_after(run_id: str, sequence: int = 0) -> list[tuple[int, dict]]:
    with connect() as db:
        rows = db.execute("SELECT sequence,event FROM events WHERE run_id=? AND sequence>? ORDER BY sequence", (run_id, sequence)).fetchall()
    return [(int(row[0]), json.loads(row[1])) for row in rows]


def enqueue(run_id: str, request: dict) -> None:
    with connect() as db:
        db.execute("INSERT INTO jobs(run_id,request,status,created_at) VALUES(?,?,?,?)",
                   (run_id, json.dumps(request), "QUEUED", time.time()))


def claim_job(lease_seconds: int = 900) -> tuple[str, dict] | None:
    now = time.time()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT run_id,request FROM jobs WHERE status='QUEUED' OR (status='RUNNING' AND lease_until<?) ORDER BY created_at LIMIT 1", (now,)).fetchone()
        if not row:
            return None
        db.execute("UPDATE jobs SET status='RUNNING',lease_until=?,attempts=attempts+1 WHERE run_id=?",
                   (now + lease_seconds, row[0]))
        request = json.loads(row[1])
    return row[0], request


def acknowledge_job(run_id: str) -> None:
    with connect() as db:
        db.execute("UPDATE jobs SET status='DONE',lease_until=NULL WHERE run_id=?", (run_id,))


def renew_job_lease(run_id: str, lease_seconds: int = 900) -> bool:
    with connect() as db:
        cursor = db.execute("UPDATE jobs SET lease_until=? WHERE run_id=? AND status='RUNNING'",
                            (time.time() + lease_seconds, run_id))
        return cursor.rowcount == 1


def cancel_job(run_id: str) -> bool:
    with connect() as db:
        cursor = db.execute("UPDATE jobs SET cancel_requested=1 WHERE run_id=? AND status IN ('QUEUED','RUNNING')", (run_id,))
        return cursor.rowcount > 0


def job_cancel_requested(run_id: str) -> bool:
    with connect() as db:
        row = db.execute("SELECT cancel_requested FROM jobs WHERE run_id=?", (run_id,)).fetchone()
    return bool(row and row[0])


def requeue_stale_jobs() -> None:
    now = time.time()
    with connect() as db:
        db.execute("UPDATE jobs SET status='QUEUED',lease_until=NULL WHERE status='RUNNING' AND lease_until<?", (now,))


def recover_queued_runs() -> None:
    """Restore interrupted RUNNING records to CREATED before a durable retry."""
    with connect() as db:
        rows = db.execute("SELECT r.id,r.payload FROM runs r JOIN jobs j ON j.run_id=r.id WHERE j.status='QUEUED'").fetchall()
    for run_id, payload in rows:
        run = RunRecord.model_validate_json(payload)
        if run.status == "RUNNING":
            run.status = "CREATED"
            run.error = None
            save(run)
            append_event(run_id, {"type": "run.requeued", "phase": "CREATED", "reason": "worker_lease_expired"})


def recover_interrupted() -> None:
    """Mark work lost on a single-process API restart as failed, never successful."""
    with connect() as db:
        rows = db.execute("SELECT payload FROM runs").fetchall()
    for (payload,) in rows:
        run = RunRecord.model_validate_json(payload)
        if run.status in {"CREATED", "RUNNING"}:
            from datetime import datetime, timezone
            run.status = "FAILED"
            run.error = "API restarted before this assessment completed; rerun to collect new evidence."
            run.completed_at = datetime.now(timezone.utc)
            save(run)
            append_event(run.id, {"type": "run.failed", "phase": "FAILED", "reason": "process_restart"})
