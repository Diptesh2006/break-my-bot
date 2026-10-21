from __future__ import annotations

import json
import os
import sqlite3
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
