"""Session 9 unit tests: the Send fan-out and the synthesizer's filtering
logic, both testable without a live LLM call."""

from unittest.mock import patch

from langgraph.types import Send

from app.graph.nodes import dispatcher as dispatcher_module


def _base_state(**overrides) -> dict:
    state = {
        "thread_id": "t1",
        "ticket_text": "My Wi-Fi is down and my account seems locked",
        "category": "network",
        "response": None,
        "messages": [],
        "iteration_count": 0,
        "seen_tool_calls": [],
        "escalated": False,
        "summary": None,
        "injection_blocked": False,
        "specialist_findings": {},
        "next_action": None,
        "delegation_count": 0,
        "dispatch_categories": [],
        "target_category": None,
    }
    state.update(overrides)
    return state


def test_dispatch_to_specialists_returns_one_send_per_category():
    state = _base_state(dispatch_categories=["network", "account"])
    sends = dispatcher_module.dispatch_to_specialists(state)

    assert len(sends) == 2
    assert all(isinstance(s, Send) for s in sends)
    assert {s.node for s in sends} == {"parallel_specialist_worker"}
    assert {s.arg["target_category"] for s in sends} == {"network", "account"}


def test_dispatch_to_specialists_handles_empty_category_list():
    state = _base_state(dispatch_categories=[])
    assert dispatcher_module.dispatch_to_specialists(state) == []


def test_synthesizer_filters_out_stale_findings_from_earlier_turns():
    """specialist_findings accumulates across turns (merge_findings never
    resets it) — the synthesizer must only consider this turn's
    dispatch_categories, or a stale finding from an unrelated earlier
    question would leak into a new response."""
    state = _base_state(
        dispatch_categories=["hardware"],
        specialist_findings={
            "network": "STALE: Wi-Fi issue from a previous, unrelated ticket.",
            "hardware": "Printer on 3rd floor is jammed, logged a facilities ticket.",
        },
    )

    class FakeResult:
        response = "Synthesized: printer ticket logged."

    with patch.object(dispatcher_module, "get_synthesizer_llm") as get_llm:
        get_llm.return_value.with_structured_output.return_value.invoke.return_value = FakeResult()
        dispatcher_module.synthesizer_node(state)

    invoke_call = get_llm.return_value.with_structured_output.return_value.invoke
    prompt_messages = invoke_call.call_args[0][0]
    human_prompt = prompt_messages[-1].content

    assert "printer" in human_prompt.lower()
    assert "STALE" not in human_prompt
