import logging

from langchain_core.messages import HumanMessage

from ...guardrails.patterns import detect_hedging, detect_injection, redact_pii
from ..state import TicketState

logger = logging.getLogger("guardrails")

BLOCKED_RESPONSE_TEXT = (
    "This request was blocked by our security guardrails and could not be "
    "processed automatically. If this was a legitimate request, please "
    "rephrase it or contact IT support directly."
)


def ingress_guardrail_node(state: TicketState) -> dict:
    """Runs before classify/agent ever see this turn's text.

    PII is redacted first (so even a blocked/logged ticket never carries raw
    PII), then the redacted text is checked for prompt injection. This node
    — not the caller — is what turns ticket_text into the persisted
    HumanMessage, so the raw text is never what reaches history or an LLM.
    """
    raw_text = state["ticket_text"]
    redacted_text, pii_found = redact_pii(raw_text)
    if pii_found:
        logger.warning("[guardrail:pii] redacted %s in incoming ticket text", sorted(set(pii_found)))

    injection_pattern = detect_injection(redacted_text)
    human_message = HumanMessage(content=redacted_text)

    if injection_pattern:
        logger.warning("[guardrail:injection] blocked ticket — matched pattern %r", injection_pattern)
        return {
            "ticket_text": redacted_text,
            "messages": [human_message],
            "injection_blocked": True,
        }

    return {
        "ticket_text": redacted_text,
        "messages": [human_message],
        "injection_blocked": False,
    }


def route_after_ingress(state: TicketState) -> str:
    return "blocked_response" if state.get("injection_blocked") else "classify"


def blocked_response_node(state: TicketState) -> dict:
    return {"response": BLOCKED_RESPONSE_TEXT}


def egress_guardrail_node(state: TicketState) -> dict:
    """Runs after finalize, before the response is handed back to the caller."""
    response = state.get("response") or ""
    redacted_response, pii_found = redact_pii(response)
    if pii_found:
        logger.warning(
            "[guardrail:pii-egress] redacted %s from outgoing response before delivery",
            sorted(set(pii_found)),
        )

    hedging = detect_hedging(redacted_response)
    if hedging:
        logger.info("[guardrail:hedging] response contains hedging markers: %s", hedging)

    return {"response": redacted_response}
