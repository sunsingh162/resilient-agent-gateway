import os
from typing import Literal

from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from ..state import TicketState

_llm: ChatAnthropic | None = None


def get_classify_llm() -> ChatAnthropic:
    global _llm
    if _llm is None:
        # The Anthropic API now rejects an explicit `temperature` for this
        # model too ("Extra inputs are not permitted") — same treatment as
        # agent.py. Structured output still constrains it to one category.
        _llm = ChatAnthropic(model=os.getenv("CLASSIFY_MODEL", "claude-haiku-4-5-20251001"))
    return _llm


class Classification(BaseModel):
    category: Literal["password", "network", "hardware", "account", "unknown"] = Field(
        description=(
            "The single best-fit category for this campus IT helpdesk ticket. "
            "Use 'unknown' only if none of the other categories clearly apply."
        )
    )


CLASSIFY_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "You are a triage classifier for a university IT helpdesk. Categories:\n"
            "- password: login failures, forgotten/expired passwords, MFA resets\n"
            "- network: Wi-Fi, VPN, campus network connectivity issues\n"
            "- hardware: broken/malfunctioning university-owned devices, printers, lab equipment\n"
            "- account: account creation, permissions, account deletion/deactivation requests\n"
            "- unknown: anything that clearly doesn't fit the above\n"
            "Classify the ticket into exactly one category.",
        ),
        ("human", "{ticket_text}"),
    ]
)


def classify_node(state: TicketState) -> dict:
    llm = get_classify_llm().with_structured_output(Classification)
    chain = CLASSIFY_PROMPT | llm
    result: Classification = chain.invoke({"ticket_text": state["ticket_text"]})
    return {"category": result.category}
