"""Phase 4, Step 4: durable state for async analysis jobs.

SQLite, same pattern as graph/approvals.py, so a job's result outlives both
the client that submitted it (the Step 4 checklist) and the server process
that ran it — poll /jobs/{id} after a restart and a finished job is still
there. What doesn't survive a restart is a job *mid-run*: the worker thread
dies with the process, so on startup any job left queued/running is marked
failed with an explicit reason instead of sitting "running" forever.

Status lifecycle:

    queued → running → completed | awaiting_approval | blocked | failed

The terminal statuses mirror /analyze's status field, plus "failed".
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from .paths import resolve_data_path

TERMINAL_STATUSES = ("completed", "awaiting_approval", "blocked", "failed")


def _default_db_path() -> str:
    checkpoint_path = resolve_data_path(os.getenv("CHECKPOINT_DB_PATH", "backend/data/checkpoints.db"))
    directory = os.path.dirname(checkpoint_path)
    return os.path.join(directory, "jobs.db") if directory else "jobs.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_connection(db_path: str | None = None) -> sqlite3.Connection:
    raw_path = db_path or os.getenv("JOBS_DB_PATH") or _default_db_path()
    path = resolve_data_path(raw_path)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT NOT NULL,
            result_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        )
        """
    )
    conn.commit()
    return conn


def _row_to_job(row: sqlite3.Row) -> dict:
    job = dict(row)
    job["result"] = json.loads(job.pop("result_json")) if job.get("result_json") else None
    return job


def create_job(thread_id: str, message: str, db_path: str | None = None) -> dict:
    job_id = f"job-{uuid.uuid4()}"
    conn = _get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO jobs (job_id, thread_id, message, status, created_at) VALUES (?, ?, ?, 'queued', ?)",
            (job_id, thread_id, message, _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return get_job(job_id, db_path)


def mark_running(job_id: str, db_path: str | None = None) -> None:
    conn = _get_connection(db_path)
    try:
        conn.execute("UPDATE jobs SET status = 'running', started_at = ? WHERE job_id = ?", (_now(), job_id))
        conn.commit()
    finally:
        conn.close()


def mark_finished(job_id: str, status: str, result: dict | None = None, error: str | None = None, db_path: str | None = None) -> None:
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"not a terminal job status: {status!r}")
    conn = _get_connection(db_path)
    try:
        conn.execute(
            "UPDATE jobs SET status = ?, result_json = ?, error = ?, finished_at = ? WHERE job_id = ?",
            (status, json.dumps(result) if result is not None else None, error, _now(), job_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_job(job_id: str, db_path: str | None = None) -> dict | None:
    conn = _get_connection(db_path)
    try:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None
    finally:
        conn.close()


def list_jobs(limit: int = 50, since: str | None = None, db_path: str | None = None) -> list[dict]:
    """Most recent first — the monitoring daemon (Step 7) reads this, with
    `since` (ISO 8601) as its rolling window on created_at."""
    conn = _get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
            (since or "", limit),
        ).fetchall()
        return [_row_to_job(row) for row in rows]
    finally:
        conn.close()


def fail_orphaned_jobs(db_path: str | None = None) -> int:
    """Called once at server startup. A queued/running job at that point
    belonged to a previous process whose worker thread is gone — it will
    never finish, so say so rather than leave a poller waiting forever."""
    conn = _get_connection(db_path)
    try:
        cursor = conn.execute(
            "UPDATE jobs SET status = 'failed', error = ?, finished_at = ? WHERE status IN ('queued', 'running')",
            ("server restarted before this job finished — resubmit it", _now()),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()
