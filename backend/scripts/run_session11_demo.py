"""Manual checklist runner for Session 11.

Usage: python backend/scripts/run_session11_demo.py
Requires ANTHROPIC_API_KEY in your environment or a .env file at repo root.

Part A (deterministic — this is what PASS/FAIL is based on): seeds a
pending write directly (same technique as tests/test_session11_hitl.py)
and drives it through pause -> bypass attempt -> approve, plus a second
seeded write through pause -> deny. This doesn't depend on whether a live
agent decides to propose a ticket for a given scenario — Claude 5 doesn't
support temperature=0, so that decision isn't fully deterministic, and a
demo built on it would occasionally "fail" for reasons that have nothing to
do with whether the approval gate works.

Part B (illustrative only, not scored): runs a real ticket through the
full live pipeline so you can see the agent actually deciding to draft a
ticket and the same gate pausing it for real. Whether it proposes a ticket
this run is the model's live judgment call, not something this script
controls.

Uses a scratch TICKETS_DB_PATH and in-memory checkpointers so this doesn't
touch your real project data or require restarting a server.
"""

import os
import sys
import tempfile
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_scratch_db = tempfile.NamedTemporaryFile(suffix="_session11_demo_tickets.db", delete=False)
os.environ["TICKETS_DB_PATH"] = _scratch_db.name

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import Command  # noqa: E402

from app.graph.graph import build_graph  # noqa: E402
from app.graph.nodes.hitl import hitl_gate_node  # noqa: E402
from app.graph.state import TicketState, new_turn_state  # noqa: E402
from app.tools.ticket_store import count_tickets_for_thread  # noqa: E402
from app.tools.write_ticket import create_ticket  # noqa: E402

URGENT_HARDWARE_TICKET = (
    "This is urgent: the printer on the 3rd floor library is completely broken "
    "and I need it fixed before my exam deadline today."
)


def build_hitl_only_graph(checkpointer):
    graph = StateGraph(TicketState)
    graph.add_node("hitl_gate", hitl_gate_node)
    graph.add_edge(START, "hitl_gate")
    graph.add_edge("hitl_gate", END)
    return graph.compile(checkpointer=checkpointer)


def seed_pending_write(thread_id: str, category: str = "hardware") -> dict:
    state = new_turn_state(thread_id, "irrelevant ticket text for this demo")
    draft = {
        "thread_id": thread_id,
        "category": category,
        "description": "Broken printer on 3rd floor, needs facilities dispatch.",
        "severity": "high",
        "student_id": None,
    }
    state["dispatch_categories"] = [category]
    state["pending_writes"] = {category: draft}
    return state


def part_a() -> bool:
    print("=" * 70)
    print("PART A — deterministic proof (this decides PASS/FAIL)")
    print("=" * 70)
    graph = build_hitl_only_graph(InMemorySaver())

    print("\n=== Step 1: a write is proposed and pauses for approval ===")
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    result = graph.invoke(seed_pending_write(thread_id), config)
    paused = bool(result.get("__interrupt__"))
    tickets_before = count_tickets_for_thread(thread_id)
    print(f"paused for approval: {paused}")
    print(f"tickets written before any decision: {tickets_before}")
    if paused:
        for i in result["__interrupt__"]:
            print(f"  pending: {i.value}")

    print("\n=== Step 2: attempt to bypass — call the tool directly ===")
    bypass_attempt = create_ticket.invoke(
        {
            "category": "hardware",
            "description": "Attempting to bypass the approval gate.",
            "severity": "critical",
            "student_id": "",
        },
        config={"configurable": {"thread_id": thread_id}},
    )
    print(f"tool result: {bypass_attempt}")
    tickets_after_bypass_attempt = count_tickets_for_thread(thread_id)
    print(f"tickets after bypass attempt: {tickets_after_bypass_attempt}")
    bypass_check_pass = (
        bypass_attempt.get("status") == "pending_approval" and tickets_after_bypass_attempt == tickets_before
    )

    print("\n=== Step 3: approve the original pending write ===")
    approved = graph.invoke(Command(resume={"action": "approve"}), config)
    tickets_after_approval = count_tickets_for_thread(thread_id)
    print(f"tickets after approval: {tickets_after_approval}")
    print(f"write_outcomes: {approved.get('write_outcomes')}")

    print("\n=== Step 4: a separate write, this time denied ===")
    thread_id_2 = str(uuid.uuid4())
    config_2 = {"configurable": {"thread_id": thread_id_2}}
    graph.invoke(seed_pending_write(thread_id_2), config_2)
    denied = graph.invoke(Command(resume={"action": "deny"}), config_2)
    tickets_after_denial = count_tickets_for_thread(thread_id_2)
    print(f"tickets after denial: {tickets_after_denial}")
    print(f"write_outcomes: {denied.get('write_outcomes')}")

    return (
        paused
        and tickets_before == 0
        and bypass_check_pass
        and tickets_after_approval == 1
        and tickets_after_denial == 0
    )


def part_b() -> None:
    print("\n" + "=" * 70)
    print("PART B — illustrative live run (not scored)")
    print("=" * 70)
    graph = build_graph(checkpointer=InMemorySaver())
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    result = graph.invoke(new_turn_state(thread_id, URGENT_HARDWARE_TICKET), config)

    if result.get("__interrupt__"):
        print("The live agent proposed a ticket and it paused for approval, as expected:")
        for i in result["__interrupt__"]:
            print(f"  pending: {i.value}")
        approved = graph.invoke(Command(resume={"action": "approve"}), config)
        print(f"\nAfter approval, tickets for this thread: {count_tickets_for_thread(thread_id)}")
        print(f"final response: {approved.get('response')}")
    else:
        print(
            "The live agent judged this not severe enough to propose a ticket this run "
            "(model output isn't fully deterministic) — response given instead:"
        )
        print(result.get("response"))


def main() -> None:
    checklist_pass = part_a()
    part_b()
    print("\nSession 11 checklist:", "PASS" if checklist_pass else "FAIL")


if __name__ == "__main__":
    main()
