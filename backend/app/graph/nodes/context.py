"""Context management: keeps the persisted message list from growing
without bound by folding older turns into a running summary once the
history crosses a threshold.
"""

import os

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import BaseMessage, HumanMessage, RemoveMessage, SystemMessage

from ..state import TicketState

SUMMARY_TRIGGER_COUNT = int(os.getenv("SUMMARY_TRIGGER_COUNT", "10"))
KEEP_RECENT_COUNT = int(os.getenv("KEEP_RECENT_COUNT", "4"))

_summarizer_llm: ChatAnthropic | None = None


def get_summarizer_llm() -> ChatAnthropic:
    global _summarizer_llm
    if _summarizer_llm is None:
        # Summarization is a cheap compression task — reuse the classify model
        # rather than the more capable (and pricier) agent model.
        _summarizer_llm = ChatAnthropic(model=os.getenv("CLASSIFY_MODEL", "claude-haiku-4-5-20251001"))
    return _summarizer_llm


def _render_transcript(messages: list[BaseMessage]) -> str:
    lines = []
    for message in messages:
        role = getattr(message, "type", message.__class__.__name__)
        text = getattr(message, "text", None) or getattr(message, "content", "")
        lines.append(f"{role}: {text}")
    return "\n".join(lines)


SUMMARIZER_SYSTEM_PROMPT = (
    "Summarize this campus IT helpdesk conversation segment in 2-4 concise "
    "sentences. Preserve concrete facts — student IDs, device names, "
    "categories, troubleshooting steps already tried, severity findings — "
    "since the raw messages will be deleted and this summary is all that "
    "remains of them."
)


def summarize_node(state: TicketState) -> dict:
    messages = state.get("messages") or []

    if len(messages) <= SUMMARY_TRIGGER_COUNT:
        return {}

    to_summarize = messages[:-KEEP_RECENT_COUNT] if KEEP_RECENT_COUNT else messages
    if not to_summarize:
        return {}

    existing_summary = state.get("summary") or ""
    transcript = _render_transcript(to_summarize)

    prompt = [
        SystemMessage(content=SUMMARIZER_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                (f"Existing summary so far: {existing_summary}\n\n" if existing_summary else "")
                + f"New segment to fold in:\n{transcript}"
            )
        ),
    ]
    summary_message = get_summarizer_llm().invoke(prompt)

    remove_ops = [RemoveMessage(id=message.id) for message in to_summarize if message.id is not None]

    return {
        "summary": summary_message.text,
        "messages": remove_ops,
    }
