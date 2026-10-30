"""Mock ticket-creation system — a small SQLite table, not a real ticketing
API. Persists so created tickets survive a restart (consistent with this
project's persistence story), and idempotency is enforced by a UNIQUE
constraint so a retried write never creates a second row.

Idempotency key = hash(thread_id + category), deliberately NOT including
the free-text description. An LLM-generated description can vary slightly
in wording between two calls for "the same" request (different tool-call
runs aren't guaranteed byte-identical text), which would defeat a
content-hash key that depends on it. Scoping identity to "this thread has
already filed a ticket for this category" is a stricter, more reliable
guarantee for this domain: at most one ticket per category per
conversation, regardless of exact phrasing.
"""

import hashlib
import os
import sqlite3
from datetime import datetime, timezone

from ..paths import resolve_data_path


def _default_db_path() -> str:
    checkpoint_path = resolve_data_path(os.getenv("CHECKPOINT_DB_PATH", "backend/data/checkpoints.db"))
    directory = os.path.dirname(checkpoint_path)
    return os.path.join(directory, "tickets.db") if directory else "tickets.db"


def _get_connection(db_path: str | None = None) -> sqlite3.Connection:
    raw_path = db_path or os.getenv("TICKETS_DB_PATH") or _default_db_path()
    path = resolve_data_path(raw_path)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT UNIQUE NOT NULL,
            thread_id TEXT NOT NULL,
            category TEXT NOT NULL,
            description TEXT NOT NULL,
            severity TEXT NOT NULL,
            student_id TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def compute_idempotency_key(thread_id: str, category: str) -> str:
    raw = f"{thread_id}:{category}".strip().lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_ticket_record(
    thread_id: str,
    category: str,
    description: str,
    severity: str,
    student_id: str | None = None,
    db_path: str | None = None,
) -> dict:
    """Creates a ticket, or returns the existing one if this thread already
    filed a ticket for this category — idempotent by construction (relying
    on the UNIQUE constraint + catching the conflict), not a
    check-then-insert race."""
    idempotency_key = compute_idempotency_key(thread_id, category)
    conn = _get_connection(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO tickets
                (idempotency_key, thread_id, category, description, severity, student_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                idempotency_key,
                thread_id,
                category,
                description,
                severity,
                student_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return {"ticket_id": cursor.lastrowid, "idempotency_key": idempotency_key, "created": True}
    except sqlite3.IntegrityError:
        row = conn.execute(
            "SELECT id FROM tickets WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return {"ticket_id": row[0], "idempotency_key": idempotency_key, "created": False}
    finally:
        conn.close()


def count_tickets_for_thread(thread_id: str, db_path: str | None = None) -> int:
    conn = _get_connection(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM tickets WHERE thread_id = ?", (thread_id,)
        ).fetchone()[0]
    finally:
        conn.close()
