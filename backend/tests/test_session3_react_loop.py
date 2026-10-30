"""Session 3 unit tests: the circuit breaker logic runs entirely without a
live LLM call for the capped/duplicate paths, so these run with no API key.
"""

from unittest.mock import patch

from langchain_core.messages import AIMessage

from app.graph.nodes import agent as agent_module


def _base_state(**overrides) -> dict:
    state = {
        "thread_id": "t1",
        "ticket_text": "test ticket",
        "category": "network",
        "response": None,
        "messages": [],
        "iteration_count": 0,
        "seen_tool_calls": [],
        "escalated": False,
    }
    state.update(overrides)
    return state


def test_iteration_cap_short_circuits_without_llm_call():
    state = _base_state(
        iteration_count=agent_module.MAX_ITERATIONS,
        messages=[AIMessage(content="previous turn")],
    )

    with patch.object(
        agent_module,
        "get_agent_llm",
        side_effect=AssertionError("LLM must not be called once the iteration cap is hit"),
    ):
        result = agent_module.agent_node(state)

    assert result["escalated"] is True
    assert result["iteration_count"] == agent_module.MAX_ITERATIONS + 1
    assert "circuit-breaker" in result["messages"][-1].content
    assert "iteration cap" in result["messages"][-1].content


def test_duplicate_tool_call_triggers_escalation_instead_of_looping():
    tool_call = {"name": "search_kb", "args": {"query": "wifi"}, "id": "call_1"}
    fingerprint = agent_module._fingerprint(tool_call)
    fake_ai_message = AIMessage(content="", tool_calls=[tool_call])

    class FakeLLM:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            return fake_ai_message

    state = _base_state(
        messages=[AIMessage(content="previous turn")],
        seen_tool_calls=[fingerprint],
    )

    with patch.object(agent_module, "get_agent_llm", return_value=FakeLLM()):
        result = agent_module.agent_node(state)

    assert result["escalated"] is True
    assert "repeated" in result["messages"][-1].content


def test_route_after_agent_goes_to_tools_when_tool_call_pending():
    state = _base_state(
        messages=[
            AIMessage(
                content="",
                tool_calls=[{"name": "search_kb", "args": {"query": "x"}, "id": "1"}],
            )
        ]
    )
    assert agent_module.route_after_agent(state) == "tools"


def test_route_after_agent_goes_to_finalize_when_escalated():
    state = _base_state(escalated=True, messages=[AIMessage(content="done")])
    assert agent_module.route_after_agent(state) == "finalize"


def test_route_after_agent_goes_to_finalize_on_plain_answer():
    state = _base_state(messages=[AIMessage(content="Here is your answer.")])
    assert agent_module.route_after_agent(state) == "finalize"
