from typing import Annotated, Literal, Optional

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

Category = Literal["password", "network", "hardware", "account", "unknown"]


def merge_findings(existing: dict[str, str], new: dict[str, str]) -> dict[str, str]:
    """Reducer for specialist_findings: parallel specialists (Session 9) each
    return a single-key dict concurrently, in the same superstep — a plain
    dict field would raise a LangGraph InvalidUpdateError on that, since it
    can't tell which concurrent write should win. Merging is safe because
    each parallel branch is assigned a distinct category key.

    This dict accumulates across turns rather than resetting (a reducer can
    combine but can't be told to "replace" without a sentinel value) — the
    synthesizer filters it down to state["dispatch_categories"] (freshly
    overwritten every turn) so a stale finding from an earlier, unrelated
    turn never leaks into a later response.
    """
    return {**(existing or {}), **(new or {})}


def merge_pending_writes(existing: dict, new: dict) -> dict:
    """Reducer for pending_writes. A category mapped to None is a clear
    signal — mirrors how RemoveMessage lets add_messages delete instead of
    append. (A custom sentinel *object* doesn't work here: every state
    update has to survive the checkpointer's msgpack serialization, and an
    arbitrary class instance isn't serializable — None is, so it's the
    sentinel.)

    Unlike specialist_findings, a stale pending_writes entry is a real bug
    risk, not just cosmetic: if a resolved (approved/denied) draft just sat
    there forever, a later, unrelated turn touching the same category could
    get re-prompted for an approval that was already handled. hitl_gate_node
    clears each entry the moment it resolves it.
    """
    merged = dict(existing or {})
    for category, value in (new or {}).items():
        if value is None:
            merged.pop(category, None)
        else:
            merged[category] = value
    return merged


class TicketState(TypedDict):
    """State for a single Campus IT Helpdesk ticket."""

    thread_id: str
    ticket_text: str
    category: Optional[Category]
    response: Optional[str]
    messages: Annotated[list, add_messages]
    iteration_count: int
    seen_tool_calls: list[str]
    escalated: bool
    summary: Optional[str]
    injection_blocked: bool
    specialist_findings: Annotated[dict[str, str], merge_findings]
    next_action: Optional[str]
    delegation_count: int
    dispatch_categories: list[str]
    target_category: Optional[str]
    pending_writes: Annotated[dict[str, dict], merge_pending_writes]
    write_outcomes: Annotated[dict[str, dict], merge_findings]


def new_turn_state(thread_id: str, ticket_text: str) -> TicketState:
    """Initial/delta state for one new turn of a ticket conversation.

    Used both to start a brand-new thread and to add a follow-up turn onto
    an existing thread — in both cases classify_node re-runs on the new
    ticket_text, and the add_messages reducer appends whatever the agent
    does onto whatever history the checkpointer already has for this
    thread_id.

    messages starts empty here deliberately: the ingress guardrail node is
    the one that turns ticket_text into a HumanMessage, only after
    redacting PII from it — so the raw, unredacted text is never what gets
    persisted into message history or sent to any LLM.
    """
    return {
        "thread_id": thread_id,
        "ticket_text": ticket_text,
        "category": None,
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
        "pending_writes": {},
        "write_outcomes": {},
    }
