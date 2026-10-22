from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from .models import ProjectRecord, RunRecord


def data_dir() -> Path:
    path = Path(os.getenv("BREAKMYBOT_DATA_DIR", "./data")).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def postgres_configured() -> bool:
    return bool(os.getenv("BREAKMYBOT_DATABASE_URL", "").strip()
        or os.getenv("PGHOST", "").strip())


def _migrate(db: sqlite3.Connection) -> None:
    """Apply ordered, idempotent SQLite schema migrations."""
    db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    applied = {row[0] for row in db.execute("SELECT version FROM schema_migrations")}
    if 1 not in applied:
        db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, event TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS jobs (run_id TEXT PRIMARY KEY, request TEXT NOT NULL, status TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)")
        db.execute("INSERT INTO schema_migrations(version) VALUES(1)")
        applied.add(1)
    if 2 not in applied:
        columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
        if "project_id" not in columns:
            db.execute("ALTER TABLE runs ADD COLUMN project_id TEXT")
        db.execute("INSERT INTO schema_migrations(version) VALUES(2)")


class PostgresConnection:
    """Small DB-API adapter keeping storage queries portable across SQLite/PostgreSQL."""

    def __init__(self, connection: Any):
        self.connection = connection

    def execute(self, statement: str, parameters: tuple = ()):
        cursor = self.connection.cursor()
        cursor.execute(statement.replace("?", "%s"), parameters)
        return cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type:
            self.connection.rollback()
        else:
            self.connection.commit()
        self.connection.close()
        return False


def _migrate_postgres(db: PostgresConnection) -> None:
    db.execute("SELECT pg_advisory_xact_lock(72623859790382856)")
    db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    applied = {row[0] for row in db.execute("SELECT version FROM schema_migrations").fetchall()}
    if 1 not in applied:
        db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS events (sequence BIGSERIAL PRIMARY KEY, run_id TEXT NOT NULL, event TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS jobs (run_id TEXT PRIMARY KEY, request TEXT NOT NULL, status TEXT NOT NULL, cancel_requested BOOLEAN NOT NULL DEFAULT FALSE, lease_until DOUBLE PRECISION, attempts INTEGER NOT NULL DEFAULT 0, created_at DOUBLE PRECISION NOT NULL)")
        db.execute("INSERT INTO schema_migrations(version) VALUES(1)")
    if 2 not in applied:
        db.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS project_id TEXT")
        db.execute("INSERT INTO schema_migrations(version) VALUES(2)")


def connect():
    database_url = os.getenv("BREAKMYBOT_DATABASE_URL", "").strip()
    if postgres_configured():
        if database_url and not database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("BREAKMYBOT_DATABASE_URL must be a PostgreSQL URL")
        if not database_url:
            required = ("PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD")
            missing = [name for name in required if not os.getenv(name, "").strip()]
            if missing:
                raise ValueError("PostgreSQL settings are incomplete; configure BREAKMYBOT_DATABASE_URL or "
                    f"the libpq variables: {', '.join(missing)}")
        try:
            import psycopg2
        except ImportError as exc:
            raise RuntimeError("Install BreakMyBot with the 'postgres' extra to use PostgreSQL") from exc
        if database_url:
            raw_connection = psycopg2.connect(database_url, connect_timeout=10)
        else:
            raw_connection = psycopg2.connect(host=os.environ["PGHOST"],
                port=os.getenv("PGPORT", "5432"), dbname=os.environ["PGDATABASE"],
                user=os.environ["PGUSER"], password=os.environ["PGPASSWORD"], connect_timeout=10)
        connection = PostgresConnection(raw_connection)
        try:
            _migrate_postgres(connection)
            raw_connection.commit()
        except Exception:
            raw_connection.rollback()
            raw_connection.close()
            raise
        return connection

    db = sqlite3.connect(data_dir() / "breakmybot.sqlite3", timeout=10)
    try:
        with db:
            _migrate(db)
    except Exception:
        db.close()
        raise
    return db


def save(run: RunRecord) -> None:
    payload = json.dumps(run.model_dump(mode="json"), ensure_ascii=False)
    with connect() as db:
        db.execute("INSERT INTO runs(id,created_at,payload,project_id) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,project_id=excluded.project_id",
                   (run.id, run.created_at.isoformat(), payload, run.project_id))


def save_project(project: ProjectRecord) -> None:
    payload = json.dumps(project.model_dump(mode="json"), ensure_ascii=False)
    with connect() as db:
        db.execute("INSERT INTO projects(id,created_at,payload) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                   (project.id, project.created_at.isoformat(), payload))


def get_project(project_id: str) -> ProjectRecord | None:
    with connect() as db:
        row = db.execute("SELECT payload FROM projects WHERE id=?", (project_id,)).fetchone()
    return ProjectRecord.model_validate_json(row[0]) if row else None


def list_projects() -> list[ProjectRecord]:
    with connect() as db:
        rows = db.execute("SELECT payload FROM projects ORDER BY created_at DESC").fetchall()
    return [ProjectRecord.model_validate_json(row[0]) for row in rows]


def get(run_id: str) -> RunRecord | None:
    with connect() as db:
        row = db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
    return RunRecord.model_validate_json(row[0]) if row else None


def latest(limit: int = 50, project_id: str | None = None) -> list[RunRecord]:
    with connect() as db:
        if project_id:
            rows = db.execute("SELECT payload FROM runs WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
                              (project_id, limit)).fetchall()
        else:
            rows = db.execute("SELECT payload FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [RunRecord.model_validate_json(row[0]) for row in rows]


def append_event(run_id: str, event: dict) -> int:
    with connect() as db:
        if postgres_configured():
            cursor = db.execute("INSERT INTO events(run_id,event) VALUES(?,?) RETURNING sequence", (run_id, json.dumps(event)))
            return int(cursor.fetchone()[0])
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
        if postgres_configured():
            row = db.execute("SELECT run_id,request FROM jobs WHERE status='QUEUED' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED").fetchone()
        else:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT run_id,request FROM jobs WHERE status='QUEUED' ORDER BY created_at LIMIT 1").fetchone()
        if not row:
            return None
        db.execute("UPDATE jobs SET status='RUNNING',lease_until=?,attempts=attempts+1 WHERE run_id=?",
                   (now + lease_seconds, row[0]))
        request = json.loads(row[1])
    return row[0], request


def acknowledge_job(run_id: str) -> None:
    with connect() as db:
        db.execute("UPDATE jobs SET status='DONE',request='{}',lease_until=NULL WHERE run_id=?", (run_id,))


def renew_job_lease(run_id: str, lease_seconds: int = 900) -> bool:
    with connect() as db:
        cursor = db.execute("UPDATE jobs SET lease_until=? WHERE run_id=? AND status='RUNNING'",
                            (time.time() + lease_seconds, run_id))
        return cursor.rowcount == 1


def cancel_job(run_id: str) -> bool:
    with connect() as db:
        if not postgres_configured():
            db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT status FROM jobs WHERE run_id=?", (run_id,)).fetchone()
        if not row or row[0] not in {"QUEUED", "RUNNING"}:
            return False
        if row[0] == "QUEUED":
            cancelled = "TRUE" if postgres_configured() else "1"
            db.execute(f"UPDATE jobs SET status='CANCELLED',cancel_requested={cancelled},request='{{}}' WHERE run_id=?", (run_id,))
            run_row = db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
            if run_row:
                run = RunRecord.model_validate_json(run_row[0])
                from datetime import datetime, timezone
                run.status = "CANCELLED"
                run.completed_at = datetime.now(timezone.utc)
                db.execute("UPDATE runs SET payload=? WHERE id=?",
                           (json.dumps(run.model_dump(mode="json"), ensure_ascii=False), run_id))
                db.execute("INSERT INTO events(run_id,event) VALUES(?,?)",
                           (run_id, json.dumps({"type": "run.cancelled", "phase": "CANCELLED"})))
        else:
            cancelled = "TRUE" if postgres_configured() else "1"
            db.execute(f"UPDATE jobs SET cancel_requested={cancelled} WHERE run_id=?", (run_id,))
        return True


def job_cancel_requested(run_id: str) -> bool:
    with connect() as db:
        row = db.execute("SELECT cancel_requested FROM jobs WHERE run_id=?", (run_id,)).fetchone()
    return bool(row and row[0])


def expire_stale_jobs() -> None:
    """Fail expired in-flight work rather than replaying possibly side-effecting requests."""
    now = time.time()
    with connect() as db:
        rows = db.execute("SELECT run_id FROM jobs WHERE status='RUNNING' AND lease_until<?", (now,)).fetchall()
        for (run_id,) in rows:
            row = db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
            if row:
                run = RunRecord.model_validate_json(row[0])
                if run.status in {"CREATED", "RUNNING"}:
                    from datetime import datetime, timezone
                    run.status = "FAILED"
                    run.error = "Worker lease expired; automatic replay was suppressed to avoid duplicate target side effects."
                    run.completed_at = datetime.now(timezone.utc)
                    db.execute("UPDATE runs SET payload=? WHERE id=?",
                               (json.dumps(run.model_dump(mode="json"), ensure_ascii=False), run_id))
                    db.execute("INSERT INTO events(run_id,event) VALUES(?,?)",
                               (run_id, json.dumps({"type": "run.failed", "phase": "FAILED", "reason": "worker_lease_expired"})))
            db.execute("UPDATE jobs SET status='FAILED',request='{}',lease_until=NULL WHERE run_id=?", (run_id,))

