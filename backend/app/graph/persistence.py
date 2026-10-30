"""SQLite-backed checkpointing.

A plain sqlite3 connection opened with check_same_thread=False, wrapped in
LangGraph's SqliteSaver. Pointed at a file on disk (CHECKPOINT_DB_PATH), so
a new process opening the same path picks up exactly where a previous
process left off — that's what "survives a server restart" means here.
"""

import os
import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver

from ..paths import resolve_data_path


def get_checkpointer(db_path: str | None = None) -> SqliteSaver:
    raw_path = db_path or os.getenv("CHECKPOINT_DB_PATH", "backend/data/checkpoints.db")
    path = resolve_data_path(raw_path)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    conn = sqlite3.connect(path, check_same_thread=False)
    return SqliteSaver(conn)
