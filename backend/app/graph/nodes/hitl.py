"""Human-in-the-loop write approval.

hitl_gate_node is the ONLY code path in this project that calls
ticket_store.create_ticket_record — the create_ticket tool (Session 10)
only ever drafts a proposal now (see tools/write_ticket.py's module
docstring for why the gate had to move here, out of the tool itself). This
node is native to the master graph (not invoked via a nested subgraph
.invoke()), so interrupt() here genuinely pauses the same graph the FastAPI
layer calls run_ticket()/get_state() on.
"""

import json
import logging

from langchain_core.messages import BaseMessage, ToolMessage
from langgraph.types import interrupt

from ...tools.ticket_store import create_ticket_record
from ..state import TicketState

logger = logging.getLogger("hitl")

WRITE_TOOL_NAME = "create_ticket"


def extract_pending_write(messages: list[BaseMessage]) -> dict | None:
    """Scans a list of messages (typically the delta a specialist dispatch
    just added) for a create_ticket ToolMessage proposing a draft, and
    returns that draft if found."""
    for message in messages:
        if isinstance(message, ToolMessage) and message.name == WRITE_TOOL_NAME:
            try:
                payload = json.loads(message.content)
            except (TypeError, json.JSONDecodeError):
                continue
            if payload.get("status") == "pending_approval":
                return payload.get("draft")
    return None


def hitl_gate_node(state: TicketState) -> dict:
    """Runs once per turn, after all specialist dispatch has completed.

    For every category in this turn's dispatch_categories with a pending
    draft, pauses via interrupt() and waits for an explicit
    Command(resume=...) decision — approve, deny, or edit_and_approve.
    There is no path in this function (or anywhere else in the codebase)
    that calls create_ticket_record without first going through interrupt().
    """
    pending = state.get("pending_writes") or {}
    relevant_categories = state.get("dispatch_categories") or []

    outcomes: dict[str, dict] = {}
    clears: dict[str, None] = {}

    for category in relevant_categories:
        draft = pending.get(category)
        if not draft:
            continue

        decision = interrupt(
            {
                "type": "write_approval",
                "category": category,
                "draft": draft,
            }
        )
        action = (decision or {}).get("action")
        logger.info("[hitl] resume decision for %s: %s", category, decision)

        if action == "deny":
            outcomes[category] = {"action": "deny", "result": None}
        elif action in ("approve", "edit_and_approve"):
            final_draft = dict(draft)
            if action == "edit_and_approve":
                final_draft.update((decision or {}).get("edited_fields") or {})
            result = create_ticket_record(
                thread_id=final_draft["thread_id"],
                category=final_draft["category"],
                description=final_draft["description"],
                severity=final_draft["severity"],
                student_id=final_draft.get("student_id"),
            )
            logger.info("[hitl] write executed for %s (action=%s): %s", category, action, result)
            outcomes[category] = {"action": action, "result": result}
        else:
            # Unrecognized/missing action: fail closed — treat as denied,
            # never as an implicit approval.
            outcomes[category] = {"action": "deny", "result": None, "reason": "unrecognized decision"}

        clears[category] = None

    return {"write_outcomes": outcomes, "pending_writes": clears}
