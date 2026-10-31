"""Phase 4, Step 8: embedding-based semantic cache in front of the swarm.

Where it's wired (the "cache-scope note"): main._analyze_turn — the one
function behind both POST /analyze (sync) and POST /analyze/async — so
both paths share one cache. /analyze/stream is NOT cached: a cached answer
has no agent handoffs or tool calls to stream, and replaying a fake event
sequence would misrepresent what ran. The Phase 3 /tickets/* routes are
untouched.

What may be cached is a helpdesk decision, not a tuning knob. An answer
is only reusable if it isn't about a specific person, so a request
*bypasses* the cache when it:
  - continues an existing thread (the answer depends on that history);
  - names a student ID or contains PII (the answer depends on that
    student's account — serving S1002's lock status to S1003 would be a
    privacy breach, not a cache hit);
  - trips the prompt-injection guardrail.
And a result is only *stored* if the turn completed cleanly: no pending
approval, no ticket written, no escalation — those are per-incident
actions, never a reusable answer.

Matching: bge-small-en-v1.5 embeddings (local, CPU, ~7ms per ticket),
cosine similarity against unexpired entries for the current
CACHE_POLICY_VERSION. Calibrated on helpdesk tickets: a paraphrase of the
Wi-Fi ticket scores 0.95, the same ticket plus an account question 0.88,
unrelated tickets ~0.57 — hence the 0.92 default threshold. Bump
CACHE_POLICY_VERSION whenever KB articles or prompts change, so answers
written under the old policy stop matching.

Every lookup is recorded as an event with what it cost (miss/bypass: the
turn's measured spend) or saved (hit: the original turn's spend and
latency), which is what the cost-model stats are computed from.
"""

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from . import cost_model
from .guardrails.patterns import detect_injection, redact_pii
from .paths import resolve_data_path

CACHE_ENABLED = os.getenv("SEMANTIC_CACHE_ENABLED", "true").lower() not in ("0", "false", "no")
SIMILARITY_THRESHOLD = float(os.getenv("CACHE_SIMILARITY_THRESHOLD", "0.92"))
TTL = timedelta(hours=float(os.getenv("CACHE_TTL_HOURS", "24")))
POLICY_VERSION = os.getenv("CACHE_POLICY_VERSION", "kb-v1")
EMBEDDING_MODEL = os.getenv("CACHE_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")

STUDENT_ID_PATTERN = re.compile(r"\bS\d{4}\b", re.I)

_embedder = None


def _get_embedder():
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding  # local import: ~1s load, only when the cache is used

        _embedder = TextEmbedding(EMBEDDING_MODEL, cache_dir=resolve_data_path("backend/data/models"))
    return _embedder


def embed(text: str) -> np.ndarray:
    vector = np.asarray(next(iter(_get_embedder().embed([text]))), dtype=np.float32)
    return vector / np.linalg.norm(vector)


def _default_db_path() -> str:
    checkpoint_path = resolve_data_path(os.getenv("CHECKPOINT_DB_PATH", "backend/data/checkpoints.db"))
    directory = os.path.dirname(checkpoint_path)
    return os.path.join(directory, "semantic_cache.db") if directory else "semantic_cache.db"


def _get_connection(db_path: str | None = None) -> sqlite3.Connection:
    raw_path = db_path or os.getenv("SEMANTIC_CACHE_DB_PATH") or _default_db_path()
    path = resolve_data_path(raw_path)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS entries (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            policy_version TEXT NOT NULL,
            question TEXT NOT NULL,
            embedding BLOB NOT NULL,
            response_json TEXT NOT NULL,
            cost_usd REAL NOT NULL,
            latency_ms INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            hits INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS events (
            at TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT,
            similarity REAL,
            entry_id INTEGER,
            cost_usd REAL NOT NULL DEFAULT 0,
            saved_usd REAL NOT NULL DEFAULT 0,
            saved_ms INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    return conn


@dataclass
class CacheDecision:
    status: str  # hit | miss | bypass
    reason: str
    similarity: float | None = None
    entry_id: int | None = None
    cached_response: dict | None = None
    original_latency_ms: int | None = None
    embedding: np.ndarray | None = None


def bypass_reason(message: str, is_new_thread: bool) -> str | None:
    if not CACHE_ENABLED:
        return "cache disabled"
    if not is_new_thread:
        return "follow-up turn: the answer depends on this thread's history"
    if STUDENT_ID_PATTERN.search(message):
        return "names a student ID: the answer depends on that account"
    _, pii_found = redact_pii(message)
    if pii_found:
        return f"contains PII ({', '.join(sorted(pii_found))}): never served across students"
    if detect_injection(message):
        return "prompt injection: handled by the ingress guardrail, never cached"
    return None


def _record_event(conn, decision: CacheDecision, cost_usd: float = 0.0, saved_usd: float = 0.0, saved_ms: int = 0):
    conn.execute(
        "INSERT INTO events (at, status, reason, similarity, entry_id, cost_usd, saved_usd, saved_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.now(timezone.utc).isoformat(),
            decision.status,
            decision.reason,
            decision.similarity,
            decision.entry_id,
            cost_usd,
            saved_usd,
            saved_ms,
        ),
    )


def lookup(message: str, is_new_thread: bool, db_path: str | None = None) -> CacheDecision:
    """A hit is recorded (with what it saved) right here; a miss or bypass
    is recorded by finish(), once the turn's real cost is known."""
    reason = bypass_reason(message, is_new_thread)
    if reason:
        return CacheDecision(status="bypass", reason=reason)

    query = embed(message)
    cutoff = (datetime.now(timezone.utc) - TTL).isoformat()
    conn = _get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT entry_id, embedding, response_json, cost_usd, latency_ms FROM entries WHERE policy_version = ? AND created_at >= ?",
            (POLICY_VERSION, cutoff),
        ).fetchall()
        best, best_similarity = None, -1.0
        for row in rows:
            similarity = float(np.frombuffer(row["embedding"], dtype=np.float32) @ query)
            if similarity > best_similarity:
                best, best_similarity = row, similarity

        if best is None or best_similarity < SIMILARITY_THRESHOLD:
            detail = f"best match {best_similarity:.3f} < {SIMILARITY_THRESHOLD}" if best else "cache empty"
            return CacheDecision(status="miss", reason=detail, similarity=best_similarity if best else None, embedding=query)

        decision = CacheDecision(
            status="hit",
            reason=f"similarity {best_similarity:.3f} >= {SIMILARITY_THRESHOLD}",
            similarity=best_similarity,
            entry_id=best["entry_id"],
            cached_response=json.loads(best["response_json"]),
            original_latency_ms=best["latency_ms"],
        )
        conn.execute("UPDATE entries SET hits = hits + 1 WHERE entry_id = ?", (best["entry_id"],))
        _record_event(conn, decision, saved_usd=best["cost_usd"], saved_ms=best["latency_ms"])
        conn.commit()
        return decision
    finally:
        conn.close()


def is_storable(response: dict) -> bool:
    return (
        response.get("status") == "completed"
        and not response.get("escalated")
        and not response.get("pending_writes")
        and bool(response.get("response"))
    )


def finish(decision: CacheDecision, message: str, response: dict, cost_usd: float, latency_ms: int, db_path: str | None = None) -> bool:
    """Records a miss/bypass with the turn's real cost, and stores the result
    on a miss if it's a reusable answer. Returns whether it was stored."""
    conn = _get_connection(db_path)
    try:
        _record_event(conn, decision, cost_usd=cost_usd)
        stored = decision.status == "miss" and is_storable(response)
        if stored:
            conn.execute(
                "INSERT INTO entries (policy_version, question, embedding, response_json, cost_usd, latency_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    POLICY_VERSION,
                    message,
                    decision.embedding.astype(np.float32).tobytes(),
                    json.dumps(response),
                    cost_usd,
                    latency_ms,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        conn.commit()
        return stored
    finally:
        conn.close()


def stats(db_path: str | None = None) -> dict:
    conn = _get_connection(db_path)
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS lookups,
                   SUM(status = 'hit') AS hits,
                   SUM(status = 'miss') AS misses,
                   SUM(status = 'bypass') AS bypasses,
                   COALESCE(SUM(cost_usd), 0) AS spent_usd,
                   COALESCE(SUM(saved_usd), 0) AS saved_usd,
                   COALESCE(SUM(saved_ms), 0) AS saved_ms
            FROM events
            """
        ).fetchone()
        entries = conn.execute("SELECT COUNT(*) FROM entries WHERE policy_version = ?", (POLICY_VERSION,)).fetchone()[0]
    finally:
        conn.close()

    lookups, hits, misses = row["lookups"], row["hits"] or 0, row["misses"] or 0
    spent, saved = row["spent_usd"], row["saved_usd"]
    saved_per_turn = saved / lookups if lookups else 0.0
    return {
        "lookups": lookups,
        "hits": hits,
        "misses": misses,
        "bypasses": row["bypasses"] or 0,
        # Of the traffic the cache was allowed to serve.
        "hit_rate_cacheable": round(hits / (hits + misses), 4) if hits + misses else 0.0,
        # Of all traffic, bypasses included — the honest number for the bill.
        "hit_rate_overall": round(hits / lookups, 4) if lookups else 0.0,
        "spent_usd": round(spent, 6),
        "saved_usd": round(saved, 6),
        "spend_without_cache_usd": round(spent + saved, 6),
        "savings_pct": round(100 * saved / (spent + saved), 2) if spent + saved else 0.0,
        "latency_saved_ms": row["saved_ms"],
        "projected_monthly_saved_usd": round(saved_per_turn * cost_model.MONTHLY_TURNS, 2),
        "projection_basis": f"{cost_model.MONTHLY_TURNS} turns/month at this window's savings per turn",
        "entries": entries,
        "similarity_threshold": SIMILARITY_THRESHOLD,
        "policy_version": POLICY_VERSION,
    }


def clear(db_path: str | None = None) -> None:
    conn = _get_connection(db_path)
    try:
        conn.executescript("DELETE FROM entries; DELETE FROM events;")
    finally:
        conn.close()
