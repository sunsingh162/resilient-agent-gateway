"""Session 12: time travel & forensics.

state_forensics() walks get_state_history() and flags anomalies.
find_bad_checkpoint() locates the parent checkpoint just before the
earliest flagged anomaly. apply_correction() writes a fix at a checkpoint
via update_state() — which itself creates a new checkpoint, branching from
that point without touching what came after it in the original run.
time_travel() re-invokes the graph starting from a chosen checkpoint,
continuing execution forward; the original branch's checkpoints are never
deleted, so a later state_forensics() call shows both branches.
"""

from dataclasses import dataclass, field
from typing import Any

from ..guardrails.patterns import CARD_PATTERN, EMAIL_PATTERN
from .nodes.agent import MAX_ITERATIONS

LOOP_PROXIMITY = "LOOP_PROXIMITY"
EMPTY_RESPONSE = "EMPTY_RESPONSE"
MISSED_GUARDRAIL = "MISSED_GUARDRAIL"


@dataclass
class TimelineEntry:
    checkpoint_id: str
    next: tuple
    category: Any
    response: Any
    iteration_count: int
    escalated: bool
    anomalies: list[str] = field(default_factory=list)


def _detect_anomalies(values: dict, next_steps: tuple) -> list[str]:
    anomalies = []

    if values.get("escalated") or values.get("iteration_count", 0) >= MAX_ITERATIONS:
        anomalies.append(LOOP_PROXIMITY)

    response = values.get("response")
    if response and (EMAIL_PATTERN.search(response) or CARD_PATTERN.search(response)):
        # A raw email/card pattern surviving into `response` means the
        # egress guardrail (Session 6) either didn't run or got bypassed —
        # this should be structurally impossible in the real graph, so
        # finding it is a strong corruption signal.
        anomalies.append(MISSED_GUARDRAIL)

    if next_steps == () and values.get("ticket_text") and not response:
        # The run reached the end of the graph with nothing to show for it.
        anomalies.append(EMPTY_RESPONSE)

    return anomalies


def _to_entry(snapshot) -> TimelineEntry:
    values = snapshot.values
    return TimelineEntry(
        checkpoint_id=snapshot.config["configurable"]["checkpoint_id"],
        next=snapshot.next,
        category=values.get("category"),
        response=values.get("response"),
        iteration_count=values.get("iteration_count", 0),
        escalated=values.get("escalated", False),
        anomalies=_detect_anomalies(values, snapshot.next),
    )


def state_forensics(graph, thread_id: str) -> list[TimelineEntry]:
    """Returns the full timeline for a thread, oldest first, each entry
    flagged with whatever anomalies were detected in its state."""
    config = {"configurable": {"thread_id": thread_id}}
    history = list(graph.get_state_history(config))  # newest first
    return [_to_entry(snapshot) for snapshot in reversed(history)]


def find_bad_checkpoint(graph, thread_id: str, anomaly_type: str):
    """Walks the timeline oldest-to-newest and returns
    (bad_snapshot, parent_config) for the EARLIEST checkpoint flagged with
    anomaly_type. parent_config is the checkpoint just before the bad value
    was written — the last-known-good point to correct from or branch off.
    Returns (None, None) if no such anomaly is found.
    """
    config = {"configurable": {"thread_id": thread_id}}
    history = list(graph.get_state_history(config))  # newest first
    oldest_first = list(reversed(history))

    for i, snapshot in enumerate(oldest_first):
        if anomaly_type in _detect_anomalies(snapshot.values, snapshot.next):
            parent_config = oldest_first[i - 1].config if i > 0 else None
            return snapshot, parent_config
    return None, None


def apply_correction(graph, checkpoint_config: dict, corrections: dict) -> dict:
    """Writes `corrections` at checkpoint_config via update_state(). This
    creates a NEW checkpoint branching from checkpoint_config — the
    checkpoints that came after checkpoint_config in the original run are
    untouched, just no longer part of the branch this new config points to.
    """
    return graph.update_state(checkpoint_config, corrections)


def time_travel(graph, checkpoint_config: dict, new_input: dict | None = None) -> dict:
    """Re-invokes the graph starting from checkpoint_config, continuing
    execution forward from there and producing a new branch. Passing
    new_input=None just continues with whatever state is already at that
    checkpoint (typically after apply_correction has fixed it); passing a
    dict applies it as an additional delta before continuing."""
    return graph.invoke(new_input, checkpoint_config)
