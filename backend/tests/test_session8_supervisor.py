"""Session 8 unit tests: the MAX_DELEGATIONS cap short-circuits without a
live LLM call, and routing after the supervisor's decision is correct.
"""

from unittest.mock import patch

from app.graph.nodes import supervisor as supervisor_module


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
        "summary": None,
        "injection_blocked": False,
        "specialist_findings": {},
        "next_action": None,
        "delegation_count": 0,
    }
    state.update(overrides)
    return state


def test_max_delegations_forces_finish_without_llm_call():
    state = _base_state(delegation_count=supervisor_module.MAX_DELEGATIONS)

    with patch.object(
        supervisor_module,
        "get_supervisor_llm",
        side_effect=AssertionError("supervisor LLM must not be called once the delegation cap is hit"),
    ):
        result = supervisor_module.supervisor_node(state)

    assert result["next_action"] == "FINISH"


def test_route_after_supervisor_goes_to_specialist_when_delegating():
    state = _base_state(next_action="network_specialist")
    assert supervisor_module.route_after_supervisor(state) == "specialist_worker"


def test_route_after_supervisor_goes_to_finalize_on_finish():
    state = _base_state(next_action="FINISH")
    assert supervisor_module.route_after_supervisor(state) == "finalize"


def test_finalize_from_findings_uses_single_finding_directly():
    state = _base_state(specialist_findings={"network": "Wi-Fi issue resolved via KB-102."})
    result = supervisor_module.finalize_from_findings_node(state)
    assert result["response"] == "Wi-Fi issue resolved via KB-102."


def test_finalize_from_findings_combines_multiple_findings():
    state = _base_state(
        specialist_findings={
            "network": "Wi-Fi issue resolved via KB-102.",
            "account": "Account is active, not locked.",
        }
    )
    result = supervisor_module.finalize_from_findings_node(state)
    assert "Network" in result["response"]
    assert "Account" in result["response"]
