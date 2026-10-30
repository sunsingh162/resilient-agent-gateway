"""Manual checklist runner for Session 9.

Usage: python backend/scripts/run_session9_demo.py
Requires ANTHROPIC_API_KEY in your environment or a .env file at repo root.

Sends a multi-issue ticket that should dispatch 2+ specialists in parallel,
and confirms via timing that they actually ran concurrently rather than
sequentially: total wall-clock time for the parallel dispatch step should
be close to the SLOWEST single specialist's duration, not the SUM of all of
them — the Session 9 checklist item from the Step-by-Step Guide. Run with
logging enabled so each "[parallel] ... specialist finding (Xs, ...)" line,
with its own timing, prints as it completes.
"""

import logging
import sys
import time
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

    start = time.monotonic()
    result = graph.invoke(new_turn_state(str(uuid.uuid4()), MULTI_ISSUE_TICKET))
    total_elapsed = time.monotonic() - start

    dispatched = result["dispatch_categories"]
    findings = result["specialist_findings"]

    print(f"\ndispatch_categories: {dispatched}")
    print(f"specialist_findings keys: {list(findings.keys())}")
    print(f"total wall-clock time for the whole run: {total_elapsed:.2f}s")
    print(
        "\nCompare the per-specialist durations logged above (each specialist's "
        "own elapsed time) against this total. If they ran sequentially, the "
        "total would be roughly the SUM of every specialist's duration plus "
        "triage/dispatch/synthesis overhead. If they ran concurrently, the "
        "total should be much closer to the SLOWEST single specialist's "
        "duration."
    )
    print(f"\nfinal response: {result['response']}")

    checklist_pass = len(dispatched) >= 2 and len(findings) >= 2
    print("\nSession 9 checklist:", "PASS" if checklist_pass else "FAIL", "(inspect timings above manually)")


if __name__ == "__main__":
    main()
