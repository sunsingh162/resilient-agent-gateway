"""Manual checklist runner for Session 5.

Usage: python backend/scripts/run_session5_demo.py
Requires ANTHROPIC_API_KEY in your environment or a .env file at repo root.

Seeds a synthetic 12-message conversation history (well past the default
SUMMARY_TRIGGER_COUNT of 10) onto a new turn, and confirms that after one
invoke() call the message list has actually shrunk and a summary field is
populated — the Session 5 checklist item from the Step-by-Step Guide.
"""

import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

from app.graph.graph import build_graph  # noqa: E402
from app.graph.nodes.context import KEEP_RECENT_COUNT, SUMMARY_TRIGGER_COUNT  # noqa: E402
from app.graph.state import new_turn_state  # noqa: E402


def build_synthetic_history(turns: int) -> list:
    messages = []
    for i in range(turns):
        messages.append(HumanMessage(content=f"Synthetic Wi-Fi question #{i}", id=f"synthetic-human-{i}"))
        messages.append(AIMessage(content=f"Synthetic troubleshooting answer #{i}.", id=f"synthetic-ai-{i}"))
    return messages


def main() -> None:
    print(f"SUMMARY_TRIGGER_COUNT={SUMMARY_TRIGGER_COUNT}, KEEP_RECENT_COUNT={KEEP_RECENT_COUNT}\n")

    graph = build_graph()
    synthetic_history = build_synthetic_history(6)  # 12 synthetic messages

    initial_state = new_turn_state(str(uuid.uuid4()), "One more thing - is my account locked too?")
    initial_state["messages"] = synthetic_history + initial_state["messages"]

    before_count = len(initial_state["messages"])
    print(f"messages before run: {before_count}")

    result = graph.invoke(initial_state)

    after_count = len(result["messages"])
    print(f"messages after run: {after_count}")
    print(f"summary populated: {bool(result.get('summary'))}")
    print(f"summary text: {result.get('summary')}\n")
    print(f"final response: {result['response']}")

    checklist_pass = after_count < before_count and bool(result.get("summary"))
    print("\nSession 5 checklist:", "PASS" if checklist_pass else "FAIL")


if __name__ == "__main__":
    main()
