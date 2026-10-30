import os
import uuid

import pytest
from langchain_core.messages import ToolMessage

from app.graph.graph import build_graph
from app.graph.state import new_turn_state

pytestmark = pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set; agent_node needs a live LLM call",
)


def _run(ticket_text: str) -> dict:
    graph = build_graph()
    return graph.invoke(new_turn_state(str(uuid.uuid4()), ticket_text))


def test_account_lookup_triggers_crm_tool_call():
    # S1001 (unlike S1002) isn't locked in the mock CRM data — this test is
    # about tool binding, not the Session 11 approval gate. A medium+
    # severity ticket (e.g. discovering a locked account) would pause here
    # via interrupt() instead of completing, since build_graph() has no
    # checkpointer in this test.
    result = _run("Can you check the status of student S1001's account?")
    assert not result.get("__interrupt__"), "unexpectedly hit the HITL approval gate"
    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert any(m.name == "lookup_student_account" for m in tool_messages)
    assert result["response"]


def test_troubleshooting_query_triggers_kb_tool_call():
    result = _run("My Wi-Fi keeps dropping every few minutes on campus, how do I fix it?")
    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert any(m.name == "search_kb" for m in tool_messages)
    assert result["response"]
