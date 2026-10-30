"""Entry points a server (or a script) calls to run a ticket turn.

Every call goes through the same compiled, checkpointed graph and always
passes thread_id in its config — that's what lets a conversation survive a
process restart: a new process calling run_ticket()/stream_ticket() with the
same thread_id resumes from whatever the checkpointer last persisted for it.
"""

from langgraph.types import Command

from . import approvals
from .graph import build_graph
from .persistence import get_checkpointer
from .state import new_turn_state

_graph = None


def get_compiled_graph():
    global _graph
    if _graph is None:
        _graph = build_graph(checkpointer=get_checkpointer())
    return _graph


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _build_turn_input(graph, thread_id: str, ticket_text: str) -> dict:
    config = _config(thread_id)
    snapshot = graph.get_state(config)
    is_new_thread = not snapshot.values.get("messages")

    if is_new_thread:
        return new_turn_state(thread_id, ticket_text)

    # Follow-up turn: only send the delta. category/response/iteration_count/
    # seen_tool_calls/escalated/next_action/delegation_count/dispatch_categories
    # reset fresh for this new question. messages is omitted entirely —
    # existing history is preserved by the checkpointer, and the ingress
    # guardrail node adds this turn's (redacted) HumanMessage as its own
    # delta once it runs.
    #
    # specialist_findings is deliberately NOT reset here — it's a
    # merge-reducer field (see state.merge_findings) that can only combine,
    # never replace, so old entries just sit unused. The synthesizer filters
    # findings down to this turn's dispatch_categories, so stale entries
    # from an earlier question never leak into a new response.
    return {
        "ticket_text": ticket_text,
        "category": None,
        "response": None,
        "iteration_count": 0,
        "seen_tool_calls": [],
        "escalated": False,
        "injection_blocked": False,
        "next_action": None,
        "delegation_count": 0,
        "dispatch_categories": [],
        "target_category": None,
    }


def run_ticket(thread_id: str, ticket_text: str) -> dict:
    graph = get_compiled_graph()
    config = _config(thread_id)
    turn_input = _build_turn_input(graph, thread_id, ticket_text)
    result = graph.invoke(turn_input, config)
    approvals.sync_from_result(thread_id, result)
    return result


def stream_ticket(thread_id: str, ticket_text: str):
    graph = get_compiled_graph()
    config = _config(thread_id)
    turn_input = _build_turn_input(graph, thread_id, ticket_text)
    last_state = None
    for state in graph.stream(turn_input, config, stream_mode="values"):
        last_state = state
        yield state
    if last_state is not None:
        approvals.sync_from_result(thread_id, last_state)


def resolve_approval(thread_id: str, decision: dict) -> dict:
    """Resumes a paused thread with an explicit approve/deny/edit_and_approve
    decision. This is the only way a paused write can ever proceed — there's
    no run_ticket()/stream_ticket() path that bypasses it, since a paused
    thread's create_ticket_record call is blocked inside hitl_gate_node's
    interrupt(), which only ever returns a value via Command(resume=...).
    """
    graph = get_compiled_graph()
    config = _config(thread_id)
    result = graph.invoke(Command(resume=decision), config)
    approvals.sync_from_result(thread_id, result)
    return result
