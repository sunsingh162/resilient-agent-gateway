"""Manual checklist runner for Session 2.

Usage: python backend/scripts/run_session2_demo.py
Requires ANTHROPIC_API_KEY in your environment or a .env file at repo root.

Prints the classified category and every tool call made while answering two
sample tickets, so a tool call is visible in terminal output — the Session 2
checklist item from the Step-by-Step Guide.
"""

import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402

from app.graph.graph import build_graph  # noqa: E402
from app.graph.state import new_turn_state  # noqa: E402

SAMPLE_TICKETS = [
    "Can you check if student S1002's account is locked?",
    "My Wi-Fi keeps dropping every few minutes on campus, how do I fix it?",
]


def main() -> None:
    graph = build_graph()
    for ticket_text in SAMPLE_TICKETS:
        print(f"\n=== Ticket: {ticket_text!r} ===")
        result = graph.invoke(new_turn_state(str(uuid.uuid4()), ticket_text))
        print(f"category: {result['category']}")

        tool_calls_seen = False
        for message in result["messages"]:
            if isinstance(message, AIMessage) and message.tool_calls:
                for call in message.tool_calls:
                    tool_calls_seen = True
                    print(f"  -> tool call: {call['name']}({call['args']})")
            if isinstance(message, ToolMessage):
                print(f"  <- tool result [{message.name}]: {message.content}")

        print(f"tool call triggered: {tool_calls_seen}")
        print(f"final response: {result['response']}")


if __name__ == "__main__":
    main()
