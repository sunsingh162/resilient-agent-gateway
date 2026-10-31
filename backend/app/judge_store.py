"""Phase 4, Step 6: durable record of judge grades, one row per graded turn.

Phoenix also gets every grade (as span annotations, for the UI), but the
monitoring daemon (Step 7) reads its judge-score SLO from here: a local
table it can query by time window without depending on Phoenix being up.
Keyed by the helpdesk.turn span_id, so re-running the judge never
double-grades a turn.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

from .paths import resolve_data_path


def _default_db_path() -> str:
    checkpoint_path = resolve_data_path(os.getenv("CHECKPOINT_DB_PATH", "backend/data/checkpoints.db"))
    directory = os.path.dirname(checkpoint_path)
    return os.path.join(directory, "judge.db") if directory else "judge.db"


def _get_connection(db_path: str | None = None) -> sqlite3.Connection:
    raw_path = db_path or os.getenv("JUDGE_DB_PATH") or _default_db_path()
    path = resolve_data_path(raw_path)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS grades (
            span_id TEXT PRIMARY KEY,
            trace_id TEXT NOT NULL,
            thread_id TEXT,
            endpoint TEXT,
            seeded TEXT,
            correctness INTEGER NOT NULL,
            safety INTEGER NOT NULL,
            tool_use INTEGER NOT NULL,
            overall REAL NOT NULL,
            passed INTEGER NOT NULL,
            verdict_json TEXT NOT NULL,
            judge_model TEXT NOT NULL,
            turn_started_at TEXT,
            graded_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def graded_span_ids(span_ids: list[str], db_path: str | None = None) -> set[str]:
    if not span_ids:
        return set()
    conn = _get_connection(db_path)
    try:
        placeholders = ",".join("?" * len(span_ids))
        rows = conn.execute(f"SELECT span_id FROM grades WHERE span_id IN ({placeholders})", span_ids).fetchall()
        return {row["span_id"] for row in rows}
    finally:
        conn.close()


def save_grade(turn: dict, verdict: dict, judge_model: str, db_path: str | None = None) -> None:
    conn = _get_connection(db_path)
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO grades (
                span_id, trace_id, thread_id, endpoint, seeded, correctness, safety, tool_use,
                overall, passed, verdict_json, judge_model, turn_started_at, graded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                turn["span_id"],
                turn["trace_id"],
                turn.get("thread_id"),
                turn.get("endpoint"),
                turn.get("seeded"),
                verdict["correctness"]["score"],
                verdict["safety"]["score"],
                verdict["tool_use"]["score"],
                verdict["overall"],
                int(verdict["passed"]),
                json.dumps(verdict),
                judge_model,
                turn.get("started_at"),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def recent_grades(limit: int = 50, since: str | None = None, db_path: str | None = None) -> list[dict]:
    """Most recently graded first — the monitoring daemon (Step 7) reads
    this, with `since` (ISO 8601) as its rolling window on graded_at."""
    conn = _get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM grades WHERE graded_at >= ? ORDER BY graded_at DESC LIMIT ?",
            (since or "", limit),
        ).fetchall()
        grades = []
        for row in rows:
            grade = dict(row)
            grade["verdict"] = json.loads(grade.pop("verdict_json"))
            grade["passed"] = bool(grade["passed"])
            grades.append(grade)
        return grades
    finally:
        conn.close()
