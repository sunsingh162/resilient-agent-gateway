"""Manual checklist runner for Session 3.

Usage: python backend/scripts/run_session3_demo.py
Requires ANTHROPIC_API_KEY in your environment or a .env file at repo root.

Forces the iteration cap to trip deterministically: MAX_ITERATIONS is set to
1, and the ticket requires at least one tool call. Modern Claude models
batch multiple tool calls into a single turn, so a query that just asks for
"lots of lookups" isn't a reliable way to force many *iterations* — but
synthesizing a final answer after seeing tool results always takes a
separate agent turn no matter how much got batched into the first one. With
the cap at 1, that second turn is guaranteed to be blocked, so this trips
the breaker every run rather than depending on how the model happens to
batch its calls. Confirms the run stops at the cap with an escalation
message — not by timing out. This is the Session 3 checklist item from the
Step-by-Step Guide.
"""

import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402
from app.graph.state import new_turn_state  # noqa: E402

AMBIGUOUS_TICKET = (
    "Please check the account status for students S1001, S1002, and S1003, "
    "then search the knowledge base separately for 'password reset', "
    "'wifi drops', 'broken printer', and 'account deactivation', and give me "
    "a full report on every single one before answering."
)


def main() -> None:
    max_iterations = os.environ.setdefault("MAX_ITERATIONS", "1")
    print(f"MAX_ITERATIONS set to {max_iterations} for this demo run.\n")

    from app.graph.graph import build_graph  # import after env var is set
    from app.graph.nodes import agent as agent_module

    print(f"agent_module.MAX_ITERATIONS resolved to: {agent_module.MAX_ITERATIONS}\n")

    graph = build_graph()
    result = graph.invoke(new_turn_state(str(uuid.uuid4()), AMBIGUOUS_TICKET))

    print(f"category: {result['category']}")
    print(f"total iterations used: {result['iteration_count']}\n")

    for message in result["messages"]:
        if isinstance(message, AIMessage) and message.tool_calls:
            for call in message.tool_calls:
                print(f"  -> tool call: {call['name']}({call['args']})")
        if isinstance(message, ToolMessage):
            print(f"  <- tool result [{message.name}]")

    print(f"\nescalated: {result['escalated']}")
    print(f"final response: {result['response']}")

    checklist_pass = (
        result["escalated"]
        and "iteration cap" in result["response"]
        and result["iteration_count"] == agent_module.MAX_ITERATIONS + 1
    )
    print("\nSession 3 checklist:", "PASS" if checklist_pass else "FAIL")


if __name__ == "__main__":
    main()
