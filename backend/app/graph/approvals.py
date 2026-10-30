"""Persisted index of pending write approvals, across all threads.

The graph's own checkpointer (SqliteSaver) is what actually lets a paused
thread resume correctly, keyed by thread_id — that's real and durable. This
module is a much smaller, separate table that only answers "what's
currently waiting across every conversation," since SqliteSaver doesn't
expose a convenient "list every thread with a pending interrupt" query.
runner.py keeps it in sync immediately after every invoke()/resume call.
"""

import json
import os
import sqlite3

from ..paths import resolve_data_path


def _default_db_path() -> str:
    checkpoint_path = resolve_data_path(os.getenv("CHECKPOINT_DB_PATH", "backend/data/checkpoints.db"))
    directory = os.path.dirname(checkpoint_path)
    return os.path.join(directory, "approvals.db") if directory else "approvals.db"


def _get_connection(db_path: str | None = None) -> sqlite3.Connection:
    raw_path = db_path or os.getenv("APPROVALS_DB_PATH") or _default_db_path()
    path = resolve_data_path(raw_path)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_approvals (
            thread_id TEXT NOT NULL,
            category TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (thread_id, category)
        )
        """
    )
    conn.commit()
    return conn


def register_pending(thread_id: str, category: str, payload: dict, db_path: str | None = None) -> None:
    conn = _get_connection(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO pending_approvals (thread_id, category, payload_json) VALUES (?, ?, ?)",
            (thread_id, category, json.dumps(payload)),
        )
        conn.commit()
    finally:
        conn.close()


def clear_pending(thread_id: str, category: str, db_path: str | None = None) -> None:
    conn = _get_connection(db_path)
    try:
        conn.execute(
            "DELETE FROM pending_approvals WHERE thread_id = ? AND category = ?",
            (thread_id, category),
        )
        conn.commit()
    finally:
        conn.close()


def list_pending(db_path: str | None = None) -> list[dict]:
    conn = _get_connection(db_path)
    try:
        rows = conn.execute("SELECT thread_id, category, payload_json FROM pending_approvals").fetchall()
        return [{"thread_id": t, "category": c, **json.loads(p)} for t, c, p in rows]
    finally:
        conn.close()


def list_pending_for_thread(thread_id: str, db_path: str | None = None) -> list[dict]:
    conn = _get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT category, payload_json FROM pending_approvals WHERE thread_id = ?", (thread_id,)
        ).fetchall()
        return [{"category": c, **json.loads(p)} for c, p in rows]
    finally:
        conn.close()


def sync_from_result(thread_id: str, result: dict, db_path: str | None = None) -> None:
    """Replaces this thread's registry entries with exactly what `result`
    (the return value of a graph.invoke() or resume call) says is currently
    pending — called by runner.py after every invoke/resume so the registry
    never drifts from what the graph itself reports."""
    for item in list_pending_for_thread(thread_id, db_path):
        clear_pending(thread_id, item["category"], db_path)
    for interrupt_obj in result.get("__interrupt__", []):
        payload = interrupt_obj.value
        register_pending(thread_id, payload["category"], payload, db_path)
