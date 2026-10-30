"""Severity-assessment tool — write-adjacent, but makes no changes itself.

This is the tool the agent calls to decide how urgent/sensitive a ticket is.
It does not touch any record; a later session (gated write access) uses its
output to decide whether a write action needs human approval before firing.
"""

from langchain_core.tools import tool

_CRITICAL_KEYWORDS = ("delete", "deactivate", "close account", "remove account")
_HIGH_KEYWORDS = ("urgent", "immediately", "compromised", "hacked", "breach", "locked out")


@tool
def assess_severity(category: str, description: str) -> dict:
    """Assess how severe/urgent an IT helpdesk ticket is. Does not modify any
    record — it only classifies the ticket so the system knows whether a
    later write action (e.g. creating a ticket) needs human approval first.

    Returns a dict with 'severity' (one of low, medium, high, critical) and
    a short 'reason'. Account deletion/deactivation requests are always at
    least critical, matching this helpdesk's escalation policy.
    """
    text = description.lower()

    if category == "account" and any(kw in text for kw in _CRITICAL_KEYWORDS):
        return {
            "severity": "critical",
            "reason": "Account deletion/deactivation requests always require human sign-off.",
        }

    if any(kw in text for kw in _HIGH_KEYWORDS):
        return {
            "severity": "high",
            "reason": "Ticket language indicates urgency or a security concern.",
        }

    if category in ("password", "account") and "locked" in text:
        return {
            "severity": "medium",
            "reason": "Account lockout affects the student's access but isn't urgent/security-critical.",
        }

    return {
        "severity": "low",
        "reason": "Routine request with no urgency or security indicators.",
    }
