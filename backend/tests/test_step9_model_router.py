"""Phase 4 Step 9: dynamic model router + tier budgets.

No API calls: get_llm() only constructs ChatAnthropic clients (no request
until .invoke), and the ledger callback is driven with a hand-built
LLMResult. The conftest fixture gives every test its own ledger DB and
fresh tier assignments. The live check (real calls, tier swap visible in
Phoenix's llm.model_name) is scripts/run_step9_router.py.
"""

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from app import main, model_router
from app.graph.nodes import agent, classify, context, dispatcher, supervisor

client = TestClient(main.app)

T = model_router.TIER_MODELS


def test_default_tiers_reproduce_the_pre_router_swarm():
    # classify/summarize were CLASSIFY_MODEL (haiku 4.5), everything else AGENT_MODEL (sonnet 5)
    assert classify.get_classify_llm().model == T["utility"]
    assert context.get_summarizer_llm().model == T["utility"]
    assert dispatcher.get_dispatcher_llm().model == T["standard"]
    assert agent.get_agent_llm().model == T["standard"]
    assert dispatcher.get_synthesizer_llm().model == T["standard"]
    assert supervisor.get_supervisor_llm().model == T["standard"]


def test_reassigning_a_tier_changes_the_model_on_the_next_call():
    model_router.set_agent_tier("specialist", "frontier")
    model_router.set_agent_tier("classify", "economy")

    assert agent.get_agent_llm().model == T["frontier"]
    assert classify.get_classify_llm().model == T["economy"]
    assert dispatcher.get_synthesizer_llm().model == T["standard"]  # untouched


def test_routed_models_carry_router_metadata_for_the_trace():
    model_router.set_agent_tier("specialist", "frontier")

    # ChatAnthropic adds its own lc_versions entry alongside ours.
    assert agent.get_agent_llm().metadata.items() >= {
        "router.agent": "specialist",
        "router.tier": "frontier",
        "router.requested_tier": "frontier",
    }.items()


def test_env_override_sets_initial_tiers(monkeypatch):
    monkeypatch.setenv("ROUTER_AGENT_TIERS", '{"synthesizer": "frontier"}')
    model_router.reset_assignments()

    assert model_router.assignments()["synthesizer"] == "frontier"
    assert model_router.assignments()["classify"] == "utility"


@pytest.mark.parametrize("agent_name, tier", [("nonexistent", "standard"), ("specialist", "platinum")])
def test_unknown_agent_or_tier_is_rejected(agent_name, tier):
    with pytest.raises(ValueError):
        model_router.set_agent_tier(agent_name, tier)


def _complete_call(callback, *, thread_id="t-1", input_tokens=1000, output_tokens=100):
    run_id = __import__("uuid").uuid4()
    callback.on_chat_model_start({}, [[]], run_id=run_id, metadata={"thread_id": thread_id})
    message = AIMessage(content="ok", usage_metadata={"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens})
    callback.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message)]]), run_id=run_id)


def test_ledger_records_real_usage_priced_by_model():
    llm = model_router.get_llm("specialist")  # standard → claude-sonnet-5 at $2/$10
    _complete_call(llm.callbacks[0], thread_id="t-42", input_tokens=10_000, output_tokens=2_000)

    [row] = model_router.calls(thread_id="t-42")
    assert (row["agent"], row["tier"], row["model"]) == ("specialist", "standard", T["standard"])
    assert (row["input_tokens"], row["output_tokens"]) == (10_000, 2_000)
    assert row["cost_usd"] == pytest.approx(0.04)


def test_over_budget_tier_downgrades_to_the_next_cheaper_tier(monkeypatch):
    monkeypatch.setitem(model_router.TIER_BUDGETS_USD, "frontier", 0.05)
    model_router.set_agent_tier("specialist", "frontier")
    _complete_call(model_router.get_llm("specialist").callbacks[0], input_tokens=10_000, output_tokens=2_000)  # $0.08 on opus 5.5

    requested, effective = model_router.resolve_tier("specialist")
    llm = agent.get_agent_llm()

    assert (requested, effective) == ("frontier", "standard")
    assert llm.model == T["standard"]
    assert llm.metadata["router.requested_tier"] == "frontier" and llm.metadata["router.tier"] == "standard"


def test_downgrade_skips_every_exhausted_tier(monkeypatch):
    for tier in ("frontier", "standard"):
        monkeypatch.setitem(model_router.TIER_BUDGETS_USD, tier, 0.0)

    model_router.set_agent_tier("specialist", "frontier")

    assert model_router.resolve_tier("specialist") == ("frontier", "utility")


def test_all_tiers_exhausted_still_serves_on_the_cheapest(monkeypatch):
    for tier in model_router.TIER_ORDER:
        monkeypatch.setitem(model_router.TIER_BUDGETS_USD, tier, 0.0)

    assert model_router.resolve_tier("specialist") == ("standard", "economy")


def test_router_endpoints_report_and_reassign():
    before = client.get("/router/status").json()
    assert before["assignments"]["specialist"] == {"tier": "standard", "effective_tier": "standard", "model": T["standard"]}
    assert set(before["tiers"]) == set(model_router.TIER_ORDER)
    assert before["tiers"]["frontier"]["price_per_mtok"] == {"input": 4.0, "output": 20.0}

    changed = client.put("/router/agents/specialist", json={"tier": "frontier"})

    assert changed.status_code == 200
    assert changed.json() == {"tier": "frontier", "effective_tier": "frontier", "model": T["frontier"]}
    assert agent.get_agent_llm().model == T["frontier"]


def test_router_endpoint_rejects_unknown_agent_and_tier():
    assert client.put("/router/agents/nonexistent", json={"tier": "standard"}).status_code == 404
    assert client.put("/router/agents/specialist", json={"tier": "platinum"}).status_code == 422
