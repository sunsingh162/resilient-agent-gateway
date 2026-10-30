import json
import os

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, SystemMessage

from ...tools.crm import lookup_student_account
from ...tools.kb import search_kb
from ...tools.severity import assess_severity
from ...tools.write_ticket import create_ticket
from ..state import TicketState

TOOLS = [lookup_student_account, search_kb, assess_severity, create_ticket]

MAX_ITERATIONS = int(os.getenv("MAX_ITERATIONS", "6"))

_llm: ChatAnthropic | None = None


def get_agent_llm() -> ChatAnthropic:
    global _llm
    if _llm is None:
        # claude-sonnet-5 (and newer Claude 5 family models) reject an explicit
        # `temperature` value — deprecated for this model, so we omit it.
        _llm = ChatAnthropic(model=os.getenv("AGENT_MODEL", "claude-sonnet-5"))
    return _llm


SYSTEM_PROMPT = (
    "You are a campus IT helpdesk assistant handling a '{category}' ticket. "
    "Use lookup_student_account to check the student's account/device status "
    "when a student ID is available or relevant, use search_kb to find "
    "troubleshooting steps, and use assess_severity once you understand the "
    "request to determine how urgent/sensitive it is. If assess_severity comes "
    "back medium or higher, use create_ticket to file it for staff follow-up "
    "— don't file a ticket for routine, fully self-service issues. Give one "
    "concise, helpful final response once you have what you need — do not "
    "call the same tool with the same arguments more than once."
)


def _system_prompt(category: str, summary: str | None) -> str:
    prompt = SYSTEM_PROMPT.format(category=category)
    if summary:
        prompt += (
            "\n\nSummary of earlier parts of this conversation (the original "
            f"messages were trimmed to save space): {summary}"
        )
    return prompt


def _fingerprint(tool_call: dict) -> str:
    return f"{tool_call['name']}:{json.dumps(tool_call['args'], sort_keys=True)}"


def _escalation_message(reason: str) -> AIMessage:
    return AIMessage(
        content=(
            "[circuit-breaker] This ticket needs a human agent — "
            f"{reason} Escalating instead of continuing automatically."
        )
    )


def agent_node(state: TicketState) -> dict:
    """Runs one ReAct step.

    The system prompt is regenerated from the current category on every call
    and never persisted into state["messages"] — only Human/AI/Tool messages
    are persisted, so a checkpointed conversation is a plain transcript, and
    a follow-up turn on a different category doesn't leave a stale system
    message from an earlier topic sitting in history.
    """
    iteration = state.get("iteration_count", 0) + 1

    if iteration > MAX_ITERATIONS:
        return {
            "messages": [_escalation_message(f"the agent hit its {MAX_ITERATIONS}-step iteration cap")],
            "iteration_count": iteration,
            "escalated": True,
        }

    llm_with_tools = get_agent_llm().bind_tools(TOOLS)
    persisted_messages = state.get("messages") or []
    system_message = SystemMessage(content=_system_prompt(state["category"], state.get("summary")))
    ai_message = llm_with_tools.invoke([system_message] + persisted_messages)

    if ai_message.tool_calls:
        seen = state.get("seen_tool_calls", [])
        fingerprints = [_fingerprint(tc) for tc in ai_message.tool_calls]
        if any(fp in seen for fp in fingerprints):
            return {
                "messages": [ai_message, _escalation_message("it repeated an identical tool call")],
                "iteration_count": iteration,
                "escalated": True,
            }
        return {
            "messages": [ai_message],
            "iteration_count": iteration,
            "seen_tool_calls": seen + fingerprints,
        }

    return {"messages": [ai_message], "iteration_count": iteration}


def route_after_agent(state: TicketState) -> str:
    if state.get("escalated"):
        return "finalize"
    last_message = state["messages"][-1]
    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        return "tools"
    return "finalize"


def finalize_node(state: TicketState) -> dict:
    """Extracts the final response text.

    Claude 5 models return AIMessage.content as a list of content blocks
    (e.g. thinking + text) rather than a plain string, so we use the
    `.text` accessor (extracts just the text blocks) instead of assuming
    `.content` is already a string.
    """
    last_message = state["messages"][-1]
    return {"response": last_message.text}
