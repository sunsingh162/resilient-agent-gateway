"""Session 4: does state actually survive a restart?

This opens a checkpoint DB file, writes state via update_state (no LLM call,
so no API key needed), closes that connection to simulate a process
shutting down, then opens a brand-new connection + graph against the same
file (simulating a fresh process starting up) and confirms the state for
that thread_id is still there.
"""

import uuid

from langchain_core.messages import AIMessage, HumanMessage

from app.graph.graph import build_graph
from app.graph.persistence import get_checkpointer


def test_state_survives_a_new_connection_to_the_same_db_file(tmp_path):
    db_path = str(tmp_path / "checkpoints.db")
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    checkpointer_a = get_checkpointer(db_path)
    graph_a = build_graph(checkpointer=checkpointer_a)
    graph_a.update_state(
        config,
        {
            "thread_id": thread_id,
            "ticket_text": "My Wi-Fi keeps dropping",
            "category": "network",
            "response": "Try forgetting and rejoining CampusSecure.",
            "messages": [
                HumanMessage(content="My Wi-Fi keeps dropping"),
                AIMessage(content="Try forgetting and rejoining CampusSecure."),
            ],
            "iteration_count": 1,
            "seen_tool_calls": [],
            "escalated": False,
        },
    )
    checkpointer_a.conn.close()  # simulates the server process shutting down

    # A brand-new connection + graph over the SAME file simulates the server
    # process restarting and picking the thread back up.
    checkpointer_b = get_checkpointer(db_path)
    graph_b = build_graph(checkpointer=checkpointer_b)
    snapshot = graph_b.get_state(config)

    assert snapshot.values["category"] == "network"
    assert snapshot.values["response"] == "Try forgetting and rejoining CampusSecure."
    assert len(snapshot.values["messages"]) == 2

    checkpointer_b.conn.close()


def test_unknown_thread_id_has_no_prior_state(tmp_path):
    db_path = str(tmp_path / "checkpoints.db")
    checkpointer = get_checkpointer(db_path)
    graph = build_graph(checkpointer=checkpointer)

    snapshot = graph.get_state({"configurable": {"thread_id": str(uuid.uuid4())}})

    assert not snapshot.values.get("messages")
    checkpointer.conn.close()
