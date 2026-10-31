"""Phase 4, Step 9: dynamic per-agent model router with tier budgets.

Every LLM call site in the swarm asks the router for its model by *agent
name* instead of hard-coding one. The router maps agent → cost tier →
model, and every call it hands out reports its real token usage back to a
ledger, priced by cost_model (Step 8). Each tier is a "virtual key" with a
rolling-window USD budget; a tier that's spent its budget routes its
agents down to the next cheaper tier that still has budget, so spend
degrades gracefully instead of failing or silently overrunning.

Tiers, cheapest first (models overridable per tier via env):

    economy   claude-haiku-5-5            $0.10 / $0.50 per MTok
    utility   claude-haiku-4-5            $1    / $5
    standard  claude-sonnet-5             $2    / $10
    frontier  claude-opus-5-5             $4    / $20

Default assignments reproduce the pre-router swarm exactly (classify and
summarize on utility, everything else on standard) — the router changes
nothing until someone changes a tier. CLASSIFY_MODEL / AGENT_MODEL are
still honoured as the utility / standard models, so existing .env files
keep working.

Verification hooks: each routed model carries metadata
{router.agent, router.tier, router.requested_tier} that OpenInference puts
on the LLM span in Phoenix, alongside the llm.model_name it already
records — so "which model handled this agent's call" is visible per call.
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any
from uuid import UUID

from langchain_anthropic import ChatAnthropic
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from . import cost_model
from .paths import resolve_data_path

logger = logging.getLogger("model_router")

TIER_ORDER = ["economy", "utility", "standard", "frontier"]  # cheapest → dearest

TIER_MODELS = {
    "economy": os.getenv("TIER_ECONOMY_MODEL", "claude-haiku-5-5"),
    "utility": os.getenv("TIER_UTILITY_MODEL") or os.getenv("CLASSIFY_MODEL", "claude-haiku-4-5-20251001"),
    "standard": os.getenv("TIER_STANDARD_MODEL") or os.getenv("AGENT_MODEL", "claude-sonnet-5"),
    "frontier": os.getenv("TIER_FRONTIER_MODEL", "claude-opus-5-5"),
}

# Rolling-window USD budget per tier ("virtual key").
TIER_BUDGETS_USD = {
    "economy": float(os.getenv("BUDGET_ECONOMY_USD", "1.00")),
    "utility": float(os.getenv("BUDGET_UTILITY_USD", "2.00")),
    "standard": float(os.getenv("BUDGET_STANDARD_USD", "10.00")),
    "frontier": float(os.getenv("BUDGET_FRONTIER_USD", "5.00")),
}
BUDGET_WINDOW = timedelta(hours=float(os.getenv("BUDGET_WINDOW_HOURS", "24")))

# Every LLM call site in the swarm, and the tier it runs on by default.
DEFAULT_AGENT_TIERS = {
    "classify": "utility",
    "summarize": "utility",
    "dispatcher": "standard",
    "specialist": "standard",
    "synthesizer": "standard",
    "supervisor": "standard",  # Phase 3 Session 8 path; not in the live graph
}

_assignments: dict[str, str] = {}
_assignments_lock = Lock()


def _initial_assignments() -> dict[str, str]:
    """Defaults, overridden by ROUTER_AGENT_TIERS='{"specialist": "frontier"}'."""
    assignments = dict(DEFAULT_AGENT_TIERS)
    overrides = json.loads(os.getenv("ROUTER_AGENT_TIERS", "{}") or "{}")
    for agent, tier in overrides.items():
        _validate(agent, tier)
        assignments[agent] = tier
    return assignments


def _validate(agent: str, tier: str) -> None:
    if agent not in DEFAULT_AGENT_TIERS:
        raise ValueError(f"unknown agent {agent!r}; known: {sorted(DEFAULT_AGENT_TIERS)}")
    if tier not in TIER_ORDER:
        raise ValueError(f"unknown tier {tier!r}; known: {TIER_ORDER}")


def assignments() -> dict[str, str]:
    with _assignments_lock:
        if not _assignments:
            _assignments.update(_initial_assignments())
        return dict(_assignments)


def set_agent_tier(agent: str, tier: str) -> None:
    """Runtime reassignment — takes effect on that agent's next LLM call."""
    _validate(agent, tier)
    assignments()  # make sure defaults are loaded first
    with _assignments_lock:
        _assignments[agent] = tier
    logger.info("[router] %s reassigned to tier %s (%s)", agent, tier, TIER_MODELS[tier])


def reset_assignments() -> None:
    with _assignments_lock:
        _assignments.clear()


# ---- ledger -----------------------------------------------------------------


def _default_db_path() -> str:
    checkpoint_path = resolve_data_path(os.getenv("CHECKPOINT_DB_PATH", "backend/data/checkpoints.db"))
    directory = os.path.dirname(checkpoint_path)
    return os.path.join(directory, "router_ledger.db") if directory else "router_ledger.db"


def _get_connection(db_path: str | None = None) -> sqlite3.Connection:
    raw_path = db_path or os.getenv("ROUTER_LEDGER_DB_PATH") or _default_db_path()
    path = resolve_data_path(raw_path)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS calls (
            at TEXT NOT NULL,
            thread_id TEXT,
            agent TEXT NOT NULL,
            requested_tier TEXT NOT NULL,
            tier TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            cost_usd REAL NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def record_call(
    *, agent: str, requested_tier: str, tier: str, model: str, input_tokens: int, output_tokens: int, thread_id: str | None
) -> float:
    cost = cost_model.cost_usd({model: {"input_tokens": input_tokens, "output_tokens": output_tokens}})
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT INTO calls (at, thread_id, agent, requested_tier, tier, model, input_tokens, output_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), thread_id, agent, requested_tier, tier, model, input_tokens, output_tokens, cost),
        )
        conn.commit()
    finally:
        conn.close()
    return cost


def tier_spend(since: datetime | None = None) -> dict[str, float]:
    since = since or datetime.now(timezone.utc) - BUDGET_WINDOW
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT tier, SUM(cost_usd) AS spent FROM calls WHERE at >= ? GROUP BY tier", (since.isoformat(),)
        ).fetchall()
    finally:
        conn.close()
    spent = {tier: 0.0 for tier in TIER_ORDER}
    spent.update({row["tier"]: row["spent"] for row in rows})
    return spent


def calls(thread_id: str | None = None, since: str | None = None) -> list[dict]:
    conn = _get_connection()
    try:
        query, params = "SELECT * FROM calls WHERE at >= ?", [since or ""]
        if thread_id:
            query += " AND thread_id = ?"
            params.append(thread_id)
        return [dict(row) for row in conn.execute(query + " ORDER BY at", params).fetchall()]
    finally:
        conn.close()


# ---- routing ----------------------------------------------------------------


def resolve_tier(agent: str) -> tuple[str, str]:
    """(requested_tier, effective_tier). Effective drops down TIER_ORDER past
    any tier whose budget window is spent; if every tier at or below the
    request is spent, it stays on the cheapest one rather than refusing —
    the overrun is logged and visible in /router/status."""
    requested = assignments()[agent]
    spent = tier_spend()
    index = TIER_ORDER.index(requested)
    for tier in reversed(TIER_ORDER[: index + 1]):
        if spent[tier] < TIER_BUDGETS_USD[tier]:
            if tier != requested:
                logger.warning(
                    "[router] %s: tier %s over budget ($%.4f >= $%.2f) — downgraded to %s",
                    agent, requested, spent[requested], TIER_BUDGETS_USD[requested], tier,
                )
            return requested, tier
    logger.error("[router] %s: every tier up to %s is over budget — running on %s anyway", agent, requested, TIER_ORDER[0])
    return requested, TIER_ORDER[0]


class _LedgerCallback(BaseCallbackHandler):
    """Writes each completed call's real token usage to the ledger. The
    thread_id comes from the LangGraph run metadata seen at call start."""

    def __init__(self, agent: str, requested_tier: str, tier: str, model: str):
        self.agent, self.requested_tier, self.tier, self.model = agent, requested_tier, tier, model
        self._threads: dict[UUID, str | None] = {}

    def on_chat_model_start(self, serialized, messages, *, run_id: UUID, metadata: dict | None = None, **kwargs: Any):
        self._threads[run_id] = (metadata or {}).get("thread_id")

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any):
        thread_id = self._threads.pop(run_id, None)
        usage = {}
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
                if message is not None and getattr(message, "usage_metadata", None):
                    usage = message.usage_metadata
        record_call(
            agent=self.agent,
            requested_tier=self.requested_tier,
            tier=self.tier,
            model=self.model,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            thread_id=thread_id,
        )


_models: dict[tuple[str, str, str], ChatAnthropic] = {}
_models_lock = Lock()


def get_llm(agent: str) -> ChatAnthropic:
    """The model this agent should use right now. Resolved on every call,
    so a tier reassignment or a budget downgrade applies to the very next
    LLM call; client instances are reused per (agent, tier)."""
    requested, tier = resolve_tier(agent)
    model = TIER_MODELS[tier]
    key = (agent, requested, tier)
    with _models_lock:
        if key not in _models:
            # No explicit temperature: the Claude 5 family rejects it (see
            # agent.py / classify.py from Phase 3).
            _models[key] = ChatAnthropic(
                model=model,
                callbacks=[_LedgerCallback(agent, requested, tier, model)],
                metadata={"router.agent": agent, "router.tier": tier, "router.requested_tier": requested},
            )
        return _models[key]


def status() -> dict:
    spent = tier_spend()
    effective = {agent: resolve_tier(agent)[1] for agent in assignments()}
    return {
        "assignments": {
            agent: {"tier": tier, "effective_tier": effective[agent], "model": TIER_MODELS[effective[agent]]}
            for agent, tier in assignments().items()
        },
        "tiers": {
            tier: {
                "model": TIER_MODELS[tier],
                "price_per_mtok": dict(zip(("input", "output"), cost_model.price_for(TIER_MODELS[tier]))),
                "budget_usd": TIER_BUDGETS_USD[tier],
                "spent_usd": round(spent[tier], 6),
                "remaining_usd": round(TIER_BUDGETS_USD[tier] - spent[tier], 6),
                "over_budget": spent[tier] >= TIER_BUDGETS_USD[tier],
            }
            for tier in TIER_ORDER
        },
        "budget_window_hours": BUDGET_WINDOW.total_seconds() / 3600,
    }
