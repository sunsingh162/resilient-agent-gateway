"""Cwd-independent path resolution for this project's data files.

A relative path like "backend/data/x.db" (the default this project's own
.env.example documents) resolves differently depending on the CWD the
process happens to be launched from. Running `cd backend && uvicorn
app.main:app` — the exact command this project's own docs use — resolves
that default to backend/backend/data/x.db instead of backend/data/x.db.
Resolving any relative path against the repo root (rather than leaving it
to whatever the process's CWD happens to be) makes it correct regardless
of where the process is launched from.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def resolve_data_path(path_str: str) -> str:
    path = Path(path_str)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return str(path)
