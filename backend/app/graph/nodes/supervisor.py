import logging
import os
from typing import Literal

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from ..state import TicketState
from ..subgraphs.specialist import build_specialist_subgraph

logger = logging.getLogger("supervisor")

MAX_DELEGATIONS = int(os.getenv("MAX_DELEGATIONS", "4"))

WorkerName = Literal[
    "password_specialist",
    "network_specialist",
    "hardware_specialist",
    "account_specialist",
    "FINISH",
]

_supervisor_llm: ChatAnthropic | None = None
_specialist_graph = None


def get_supervisor_llm() -> ChatAnthropic:
    global _supervisor_llm
    if _supervisor_llm is None:
        _supervisor_llm = ChatAnthropic(model=os.getenv("AGENT_MODEL", "claude-sonnet-5"))
    return _supervisor_llm


def get_specialist_graph():
    global _specialist_graph
    if _specialist_graph is None:
        _specialist_graph = build_specialist_subgraph()
    return _specialist_graph


class SupervisorDecision(BaseModel):
    next_action: WorkerName = Field(description="The next specialist to dispatch to, or FINISH.")
    reasoning: str = Field(description="One sentence explaining the choice, for logging/forensics.")


SUPERVISOR_SYSTEM_PROMPT = (
    "You are the supervisor for a campus IT helpdesk multi-agent system. Given "
    "the ticket and the specialist findings gathered so far, decide which "
    "specialist should act next, or FINISH if the ticket has already been "
    "fully addressed by the findings so far. Only dispatch a specialist whose "
    "domain (password, network, hardware, account) is actually relevant to "
    "this ticket — most tickets only need one specialist. Never dispatch the "
    "same specialist twice."
)


def supervisor_node(state: TicketState) -> dict:
    delegation_count = state.get("delegation_count", 0)

    if delegation_count >= MAX_DELEGATIONS:
        logger.warning(
            "[supervisor] MAX_DELEGATIONS (%d) reached — forcing FINISH without a further LLM call",
            MAX_DELEGATIONS,
        )
        return {"next_action": "FINISH"}

    findings = state.get("specialist_findings") or {}
    findings_text = "\n".join(f"- {category}: {text}" for category, text in findings.items()) or "(none yet)"

    prompt = [
        SystemMessage(content=SUPERVISOR_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                f"Ticket: {state['ticket_text']}\n"
                f"Initial classification: {state.get('category')}\n"
                f"Specialist findings so far:\n{findings_text}"
            )
        ),
    ]

    decision = get_supervisor_llm().with_structured_output(SupervisorDecision).invoke(prompt)

    logger.info(
        "[supervisor] decision=%s reasoning=%r (delegation %d/%d)",
        decision.next_action,
        decision.reasoning,
        delegation_count,
        MAX_DELEGATIONS,
    )

    update: dict = {"next_action": decision.next_action}
    if decision.next_action != "FINISH":
        update["delegation_count"] = delegation_count + 1
    return update


def route_after_supervisor(state: TicketState) -> str:
    return "finalize" if state.get("next_action") == "FINISH" else "specialist_worker"


def specialist_worker_node(state: TicketState) -> dict:
    """Dispatches to the Session 7 specialist subgraph, scoped to whichever
    category the supervisor just picked. Always returns to the supervisor —
    it never routes directly to END.

    iteration_count/seen_tool_calls/escalated reset for this dispatch: the
    ReAct circuit breaker (Session 3) applies per specialist call, not
    cumulatively across every specialist a multi-issue ticket might need.
    """
    worker = state["next_action"]
    category = worker.removesuffix("_specialist")

    prior_messages = state.get("messages") or []
    # A second (or later) dispatch would otherwise hand agent_node a message
    # list ending in the *previous* specialist's AIMessage — Claude 5 models
    # reject that ("must end with a user message", no assistant prefill).
    # A fresh, category-specific assignment message keeps every dispatch
    # ending on a user turn and gives the specialist a clear, focused ask.
    dispatch_message = HumanMessage(
        content=(
            f"As the {category} specialist, investigate this ticket and give "
            f"your finding: {state['ticket_text']}"
        )
    )
    sub_state = {
        **state,
        "category": category,
        "iteration_count": 0,
        "seen_tool_calls": [],
        "escalated": False,
        "response": None,
        "messages": prior_messages + [dispatch_message],
    }

    # Passing thread_id through explicitly: this is a separate compiled
    # graph object, so it does NOT automatically inherit the outer master
    # graph's invoke() config — any bound tool needing thread_id (e.g. a
    # gated write tool's idempotency key) would otherwise see none.
    result = get_specialist_graph().invoke(sub_state, {"configurable": {"thread_id": state["thread_id"]}})

    findings = dict(state.get("specialist_findings") or {})
    findings[category] = result["response"]
    logger.info("[supervisor] %s specialist finding: %s", category, result["response"])

    # Only return the messages this dispatch actually added — the outer
    # graph's add_messages reducer appends them; returning the full list
    # back would double them up.
    new_messages = result["messages"][len(prior_messages):]

    return {
        "specialist_findings": findings,
        "messages": new_messages,
        "escalated": result.get("escalated", False) or state.get("escalated", False),
    }


def finalize_from_findings_node(state: TicketState) -> dict:
    """Combines specialist findings into a response.

    Deliberately naive — no filtering or LLM-based synthesis. Session 9's
    synthesizer node is what upgrades this into something that actually
    reads and reconciles multiple findings intelligently.
    """
    findings = state.get("specialist_findings") or {}
    if len(findings) == 1:
        response = next(iter(findings.values()))
    elif findings:
        response = "\n\n".join(f"**{category.capitalize()}**: {text}" for category, text in findings.items())
    else:
        response = "No specialist findings were produced for this ticket."
    return {"response": response}
