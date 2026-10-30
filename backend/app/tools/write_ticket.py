"""The gated write tool.

As of Session 11, this tool never writes anything itself — it only drafts a
proposed ticket (after the same severity gate as before) and hands it back
as a "pending_approval" marker. The actual write happens in
graph/nodes/hitl.py's hitl_gate_node, gated behind a real interrupt() —
that's the only code path in this project that calls
ticket_store.create_ticket_record.

Why the write moved out of the tool: create_ticket runs inside a nested
compiled subgraph (the specialist ReAct loop), invoked via a plain
.invoke() call from a parent node — not LangGraph's native
add_node(compiled_subgraph) composition. interrupt() only pauses the
specific compiled graph it's called within; called here, it would pause the
inner subgraph while the outer master graph (the one a FastAPI request is
actually waiting on) sailed on none the wiser. Keeping this tool a pure,
side-effect-free "propose a draft" function and gating the real write in a
node that's native to the outer graph sidesteps that entirely.
"""

import os

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

SEVERITY_ORDER = ["low", "medium", "high", "critical"]
WRITE_GATE_MIN_SEVERITY = os.getenv("WRITE_GATE_MIN_SEVERITY", "medium")


def meets_write_threshold(severity: str) -> bool:
    try:
        return SEVERITY_ORDER.index(severity) >= SEVERITY_ORDER.index(WRITE_GATE_MIN_SEVERITY)
    except ValueError:
        return False  # unrecognized severity string — fail closed, no ticket


@tool
def create_ticket(category: str, description: str, severity: str, student_id: str, config: RunnableConfig) -> dict:
    """Propose a follow-up ticket in the IT helpdesk system for staff to action.

    This does NOT create the ticket immediately — it drafts one for human
    approval. Only call this for issues that genuinely need staff follow-up
    beyond a self-service answer — e.g. an account unlock, a hardware
    dispatch, or anything you assessed as medium severity or higher via
    assess_severity. Don't call this for routine issues fully resolved by a
    KB article.

    category: one of password, network, hardware, account.
    severity: one of low, medium, high, critical — must match what
        assess_severity returned for this issue; drafts below the
        helpdesk's configured severity threshold are refused outright.
    student_id: the student's ID if known, else an empty string.

    Returns either a refusal (severity too low) or a pending-approval
    marker with the draft — a human will approve, deny, or edit-and-approve
    it before anything is actually written.
    """
    if not meets_write_threshold(severity):
        return {
            "status": "refused",
            "reason": (
                f"Severity '{severity}' is below this helpdesk's ticket-filing "
                f"threshold ('{WRITE_GATE_MIN_SEVERITY}') — no ticket needed."
            ),
        }

    thread_id = (config or {}).get("configurable", {}).get("thread_id") or "unknown-thread"
    return {
        "status": "pending_approval",
        "draft": {
            "thread_id": thread_id,
            "category": category,
            "description": description,
            "severity": severity,
            "student_id": student_id or None,
        },
    }
