"""Phase 4, Step 1 checklist: the base swarm runs end to end from a plain
Python script, before any production infrastructure is layered on top.

Usage: python backend/scripts/run_swarm.py ["optional ticket text"]
Requires ANTHROPIC_API_KEY in your environment or a .env file at repo root.

Goes through the same entry point the gateway will wrap (runner.run_ticket,
with the SQLite checkpointer), not a bare graph.invoke() — so what this
proves is exactly what Step 2 puts behind HTTP. If a specialist drafts a
ticket write, the run pauses at the HITL gate; this script auto-approves it
so the turn can finish, and says so in its output.
"""

import sys
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.graph.runner import resolve_approval, run_ticket  # noqa: E402

DEFAULT_TICKET = (
    "My Wi-Fi keeps dropping every few minutes in the library, and I also "
    "can't remember if my student account (S1002) is locked. Can you check both?"
)


def main() -> None:
    ticket = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TICKET
    thread_id = f"step1-{uuid.uuid4()}"

    print(f"thread_id: {thread_id}")
    print(f"ticket:    {ticket}\n")

    start = time.monotonic()
    result = run_ticket(thread_id, ticket)

    # hitl_gate interrupts once per drafted write, so a multi-issue ticket
    # can pause more than once in the same turn.
    auto_approved = 0
    while result.get("__interrupt__"):
        print("[hitl] a ticket write is paused for approval — auto-approving for this smoke run")
        result = resolve_approval(thread_id, {"action": "approve"})
        auto_approved += 1
    elapsed = time.monotonic() - start

    print(f"category:            {result.get('category')}")
    print(f"dispatch_categories: {result.get('dispatch_categories')}")
    print(f"specialists run:     {sorted((result.get('specialist_findings') or {}).keys())}")
    print(f"write outcomes:      {result.get('write_outcomes') or '(none)'}")
    print(f"elapsed:             {elapsed:.1f}s")
    print(f"\nresponse:\n{result.get('response')}")

    passed = bool(result.get("response")) and not result.get("__interrupt__")
    note = f" ({auto_approved} HITL write(s) auto-approved)" if auto_approved else ""
    print(f"\nStep 1 checklist: {'PASS' if passed else 'FAIL'} — swarm ran end to end{note}")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
