"""Manual checklist runner for Session 8.

Usage: python backend/scripts/run_session8_demo.py
Requires ANTHROPIC_API_KEY in your environment or a .env file at repo root.

Sends a genuinely multi-issue ticket (a network problem AND an account
question in the same message) and confirms it visibly bounces through the
supervisor 2+ times before reaching FINISH — the Session 8 checklist item
from the Step-by-Step Guide. Run with logging enabled so the
"[supervisor] decision=..." lines print step by step.
"""

import logging
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.graph.graph import build_graph  # noqa: E402
from app.graph.state import new_turn_state  # noqa: E402

MULTI_ISSUE_TICKET = (
    "Two things: first, my Wi-Fi keeps dropping every few minutes in the library. "
    "Second, separately, can you check if my student account (S1002) is locked? "
    "I need both looked at."
)


def main() -> None:
    graph = build_graph()
    result = graph.invoke(new_turn_state(str(uuid.uuid4()), MULTI_ISSUE_TICKET))

    print(f"\nspecialist_findings: {list(result['specialist_findings'].keys())}")
    print(f"delegation_count: {result['delegation_count']}")
    print(f"final response: {result['response']}")

    checklist_pass = result["delegation_count"] >= 2 and len(result["specialist_findings"]) >= 2
    print("\nSession 8 checklist:", "PASS" if checklist_pass else "FAIL")


if __name__ == "__main__":
    main()
