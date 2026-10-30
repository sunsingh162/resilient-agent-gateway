"""Manual checklist runner for Session 12.

Usage: python backend/scripts/run_session12_demo.py
Requires ANTHROPIC_API_KEY in your environment or a .env file at repo root.

Runs a real ticket through the full live graph, then deliberately corrupts
a checkpoint — injecting raw PII into the response right at the point
between synthesizer and egress (simulating a bug where the synthesizer
leaked something the egress guardrail should catch) — and walks through
the full cycle: find the bad checkpoint programmatically, correct it, and
re-invoke (time travel) from that exact point so egress genuinely re-runs
for real, live — the Session 12 checklist item from the Step-by-Step Guide.

Uses an in-memory checkpointer so this doesn't touch your real project data.
"""

import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from app.graph.forensics import (  # noqa: E402
    MISSED_GUARDRAIL,
    apply_correction,
    find_bad_checkpoint,
    state_forensics,
    time_travel,
)
from app.graph.graph import build_graph  # noqa: E402
from app.graph.state import new_turn_state  # noqa: E402

TICKET_TEXT = "My Wi-Fi keeps dropping every few minutes in the library, how do I fix it?"


def main() -> None:
    graph = build_graph(checkpointer=InMemorySaver())
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    print("=== Step 1: run a real ticket to completion ===")
    result = graph.invoke(new_turn_state(thread_id, TICKET_TEXT), config)
    print(f"original response: {result['response']}\n")

    print("=== Step 2: deliberately corrupt a checkpoint ===")
    history = list(graph.get_state_history(config))
    # Find the checkpoint right after synthesizer ran (before egress) —
    # simulates a bug where synthesizer's own output leaked raw PII that
    # the egress guardrail was supposed to catch.
    target = next(s for s in history if s.next == ("egress",))
    print(f"corrupting checkpoint {target.config['configurable']['checkpoint_id'][:8]}... (next={target.next})")
    graph.update_state(
        target.config,
        {"response": "Contact our support lead directly at leaked.staff@university.edu for a manual fix."},
    )

    print("\n=== Step 3: run forensics — detect the corruption ===")
    timeline = state_forensics(graph, thread_id)
    flagged = [e for e in timeline if MISSED_GUARDRAIL in e.anomalies]
    print(f"timeline has {len(timeline)} checkpoints, {len(flagged)} flagged MISSED_GUARDRAIL")
    for entry in flagged:
        print(f"  checkpoint {entry.checkpoint_id[:8]}... response={entry.response!r}")
    detection_pass = len(flagged) > 0

    print("\n=== Step 4: find the bad checkpoint programmatically ===")
    bad_snapshot, parent_config = find_bad_checkpoint(graph, thread_id, MISSED_GUARDRAIL)
    print(f"bad checkpoint: {bad_snapshot.config['configurable']['checkpoint_id'][:8]}...")
    print(f"parent (last known good): {parent_config['configurable']['checkpoint_id'][:8]}...")
    print(f"parent's response was clean: {parent_config != bad_snapshot.config}")

    print("\n=== Step 5: correct the bad checkpoint ===")
    corrected_config = apply_correction(
        graph,
        bad_snapshot.config,
        {"response": "Try forgetting and rejoining the CampusSecure network, and disable Wi-Fi power-saving mode."},
    )
    print(f"corrected checkpoint: {corrected_config['configurable']['checkpoint_id'][:8]}...")

    print("\n=== Step 6: time travel — re-invoke from the corrected checkpoint ===")
    final = time_travel(graph, corrected_config)
    print(f"final response after time travel: {final['response']}")
    time_travel_pass = final["response"] and "leaked.staff" not in final["response"]

    print("\n=== Step 7: confirm the original corrupted branch still exists ===")
    full_timeline = state_forensics(graph, thread_id)
    still_has_corruption = any("leaked.staff" in (e.response or "") for e in full_timeline)
    still_has_fix = any(e.response == final["response"] for e in full_timeline)
    print(f"corrupted branch still in history: {still_has_corruption}")
    print(f"corrected branch now in history: {still_has_fix}")

    checklist_pass = detection_pass and time_travel_pass and still_has_corruption and still_has_fix
    print("\nSession 12 checklist:", "PASS" if checklist_pass else "FAIL")


if __name__ == "__main__":
    main()
