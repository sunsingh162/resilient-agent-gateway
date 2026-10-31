"""Load .env before tests run, so tests that need ANTHROPIC_API_KEY pick it
up the same way the demo scripts and the FastAPI app do, without requiring
it to be exported manually in the shell running pytest.
"""

from dotenv import load_dotenv

load_dotenv()


# Phase 4, Step 8: every /analyze call now goes through the semantic cache.
# Isolate it per test (its own tmp DB) and keep it off by default, so tests
# never touch backend/data/semantic_cache.db, never load the embedding model,
# and one test's stored answer can't become another test's cache hit.
# Step 8's own tests switch it back on explicitly.
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_semantic_cache(tmp_path, monkeypatch):
    from app import semantic_cache

    monkeypatch.setenv("SEMANTIC_CACHE_DB_PATH", str(tmp_path / "semantic_cache.db"))
    monkeypatch.setattr(semantic_cache, "CACHE_ENABLED", False)


# Phase 4, Step 9: every LLM call now goes through the model router, which
# writes a ledger row per call. Point the ledger at a per-test tmp DB and
# reset tier assignments, so tests never write to backend/data and one
# test's tier change or spend can't leak into another.
@pytest.fixture(autouse=True)
def _isolated_model_router(tmp_path, monkeypatch):
    from app import model_router

    monkeypatch.setenv("ROUTER_LEDGER_DB_PATH", str(tmp_path / "router_ledger.db"))
    model_router.reset_assignments()
    yield
    model_router.reset_assignments()
