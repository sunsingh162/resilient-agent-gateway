"""Pure, regex-based guardrail logic — no LLM, no graph dependency.

This is a deterministic, pattern-matching mock guardrail layer, not a full
PII-NER or ML-based injection classifier — that's an explicit scope choice
(structured PII patterns + known jailbreak phrasings are demonstrable and
testable; freeform entity recognition is a different, much larger problem
that's out of scope for this phase).
"""

import re

INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(?:all\s+|any\s+|previous\s+|prior\s+|the\s+)*(instructions|prompts)", re.I),
    re.compile(r"disregard (the |your )?(system|previous|prior) prompt", re.I),
    re.compile(r"reveal (your|the) (system prompt|instructions)", re.I),
    re.compile(r"you are now (?!handling)", re.I),
    re.compile(r"pretend (you|to) (are|be)", re.I),
    re.compile(r"\bDAN\b"),  # "Do Anything Now" jailbreak meme
    re.compile(r"forget (all|everything) (you|above)", re.I),
]

EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CARD_PATTERN = re.compile(r"\b(?:\d{4}[ -]?){3}\d{1,4}\b")
PHONE_PATTERN = re.compile(r"\b(?:\+?\d{1,3}[-.\s])?\(?\d{2,4}\)?[-.\s]\d{3,4}[-.\s]\d{3,4}\b")
NAME_DECLARATION_PATTERN = re.compile(
    r"\b(my name is|i am|i'm)\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)", re.I
)

HEDGING_PHRASES = [
    "i'm not sure",
    "i am not sure",
    "i think",
    "i believe",
    "possibly",
    "might be",
    "as an ai",
    "i cannot guarantee",
    "it's possible that",
]


def detect_injection(text: str) -> str | None:
    """Returns the matched pattern string if text looks like a prompt-injection
    attempt, else None."""
    for pattern in INJECTION_PATTERNS:
        if pattern.search(text):
            return pattern.pattern
    return None


def redact_pii(text: str) -> tuple[str, list[str]]:
    """Redacts emails, card numbers, phone numbers, and self-declared names.
    Returns (redacted_text, list of PII type labels found)."""
    found: list[str] = []

    def _mark(label: str):
        def repl(match: re.Match) -> str:
            found.append(label)
            return f"[REDACTED_{label}]"

        return repl

    text = EMAIL_PATTERN.sub(_mark("EMAIL"), text)
    text = CARD_PATTERN.sub(_mark("CARD"), text)
    text = PHONE_PATTERN.sub(_mark("PHONE"), text)

    def _name_repl(match: re.Match) -> str:
        found.append("NAME")
        return f"{match.group(1)} [REDACTED_NAME]"

    text = NAME_DECLARATION_PATTERN.sub(_name_repl, text)

    return text, found


def detect_hedging(text: str) -> list[str]:
    """Returns the list of hedging/uncertainty phrases found in text."""
    lowered = text.lower()
    return [phrase for phrase in HEDGING_PHRASES if phrase in lowered]
