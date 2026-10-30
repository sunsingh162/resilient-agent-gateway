"""Session 12 tests: anomaly detection, and the full corrupt -> detect ->
correct -> time-travel cycle. All deterministic — no LLM call needed, since
these tests seed state directly via update_state() rather than relying on
live agent decisions (same lesson learned the hard way in Session 11).
"""

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.graph.forensics import (
    EMPTY_RESPONSE,
    LOOP_PROXIMITY,
    MISSED_GUARDRAIL,
    apply_correction,
    find_bad_checkpoint,
    state_forensics,
    time_travel,
)
from app.graph.nodes.agent import MAX_ITERATIONS
from app.graph.state import TicketState, new_turn_state

# --- pure detector tests -----------------------------------------------


def test_loop_proximity_flags_escalated_state():
    from app.graph.forensics import _detect_anomalies

    assert LOOP_PROXIMITY in _detect_anomalies({"escalated": True}, next_steps=())
    assert LOOP_PROXIMITY in _detect_anomalies({"iteration_count": MAX_ITERATIONS}, next_steps=())
    assert LOOP_PROXIMITY not in _detect_anomalies({"iteration_count": 1, "escalated": False}, next_steps=())


def test_missed_guardrail_flags_raw_pii_in_response():
    from app.graph.forensics import _detect_anomalies

    values = {"response": "Contact me at leaked@example.com for details."}
    assert MISSED_GUARDRAIL in _detect_anomalies(values, next_steps=())

    clean_values = {"response": "Try forgetting and rejoining CampusSecure."}
    assert MISSED_GUARDRAIL not in _detect_anomalies(clean_values, next_steps=())


def test_empty_response_flags_terminal_state_with_no_response():
    from app.graph.forensics import _detect_anomalies

    values = {"ticket_text": "My Wi-Fi is down", "response": None}
    assert EMPTY_RESPONSE in _detect_anomalies(values, next_steps=())
    # Not terminal yet (next is non-empty) — no response is expected mid-run.
    assert EMPTY_RESPONSE not in _detect_anomalies(values, next_steps=("agent",))


# --- full mechanic tests, using a minimal graph -------------------------


def _build_test_graph(checkpointer):
    def node_ok(state):
        return {"response": "A safe, already-redacted response.", "category": "network", "iteration_count": 1}

    def node_finish(state):
        return {}

    graph = StateGraph(TicketState)
    graph.add_node("node_ok", node_ok)
    graph.add_node("node_finish", node_finish)
    graph.add_edge(START, "node_ok")
    graph.add_edge("node_ok", "node_finish")
    graph.add_edge("node_finish", END)
    return graph.compile(checkpointer=checkpointer)


def test_full_corrupt_detect_correct_time_travel_cycle():
    graph = _build_test_graph(InMemorySaver())
    thread_id = "forensics-test-thread"
    config = {"configurable": {"thread_id": thread_id}}

    graph.invoke(new_turn_state(thread_id, "My Wi-Fi is down"), config)

    # Deliberately corrupt the completed run: simulate a bug where raw PII
    # leaked into the final response despite the (Session 6) egress guardrail.
    latest = graph.get_state(config)
    graph.update_state(latest.config, {"response": "Contact me at leaked@example.com about this."})

    # 1. Detect the corruption.
    timeline = state_forensics(graph, thread_id)
    assert any(MISSED_GUARDRAIL in entry.anomalies for entry in timeline)

    # 2. Find the bad checkpoint and its last-known-good parent.
    bad_snapshot, parent_config = find_bad_checkpoint(graph, thread_id, MISSED_GUARDRAIL)
    assert bad_snapshot is not None
    assert parent_config is not None
    assert "leaked@example.com" not in graph.get_state(parent_config).values.get("response", "")

    # 3. Correct it at the parent (last-known-good) checkpoint.
    corrected_config = apply_correction(
        graph, parent_config, {"response": "Try forgetting and rejoining CampusSecure."}
    )

    # 4. Time travel: continue from the corrected checkpoint.
    result = time_travel(graph, corrected_config)
    assert result["response"] == "Try forgetting and rejoining CampusSecure."

    # 5. The original (corrupted) branch must still exist — not destroyed.
    full_timeline = state_forensics(graph, thread_id)
    assert any("leaked@example.com" in (e.response or "") for e in full_timeline)
    assert any(e.response == "Try forgetting and rejoining CampusSecure." for e in full_timeline)


def test_find_bad_checkpoint_returns_none_when_nothing_is_wrong():
    graph = _build_test_graph(InMemorySaver())
    thread_id = "forensics-clean-thread"
    config = {"configurable": {"thread_id": thread_id}}
    graph.invoke(new_turn_state(thread_id, "My Wi-Fi is down"), config)

    bad_snapshot, parent_config = find_bad_checkpoint(graph, thread_id, MISSED_GUARDRAIL)
    assert bad_snapshot is None
    assert parent_config is None
