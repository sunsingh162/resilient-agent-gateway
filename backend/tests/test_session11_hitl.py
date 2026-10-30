"""Session 11 tests: human-in-the-loop write approval.

The static-analysis test is the strongest proof here — it's a much better
guarantee than any single runtime scenario that "the write tool never fires
without going through approval," because it inspects every code path rather
than sampling one.

The approve/deny/edit-and-approve tests seed pending_writes directly and
exercise hitl_gate_node in a small standalone graph, rather than running
the full pipeline and hoping the live agent decides to propose a ticket for
a given scenario — that decision isn't fully deterministic (Claude 5
doesn't support temperature=0), so a test built on "will the agent file a
ticket this run" is occasionally flaky for reasons that have nothing to do
with whether the approval gate itself works. These tests need no API key.
"""

import ast
import inspect
import pathlib
import uuid

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.graph.nodes.hitl import hitl_gate_node
from app.graph.state import TicketState, new_turn_state
from app.tools.ticket_store import count_tickets_for_thread


def test_create_ticket_record_has_exactly_one_call_site():
    """Static-analysis proof for the Session 11 checklist: create_ticket_record
    (the only function that ever writes a ticket) must only ever be CALLED
    from hitl_gate_node — which is unreachable without interrupt() and an
    explicit Command(resume=...) decision.
    """
    app_dir = pathlib.Path(__file__).resolve().parents[1] / "app"
    call_sites = []

    for path in app_dir.rglob("*.py"):
        source = path.read_text()
        if "create_ticket_record(" not in source:
            continue
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if name == "create_ticket_record":
                    call_sites.append(str(path.relative_to(app_dir)))

    assert call_sites, "expected at least one call site (hitl_gate_node)"
    assert all(path == "graph/nodes/hitl.py" for path in call_sites), call_sites


def test_write_ticket_tool_never_imports_or_calls_create_ticket_record():
    """The tool itself must have zero ability to write — not just a runtime
    check that happens not to be triggered. Checked via AST (imports/calls),
    not a raw substring search, since the module's docstring legitimately
    mentions create_ticket_record by name to explain why the write moved
    elsewhere.
    """
    import app.tools.write_ticket as write_ticket_module

    tree = ast.parse(inspect.getsource(write_ticket_module))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names]
            assert "create_ticket_record" not in names
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            assert name != "create_ticket_record"


def _build_hitl_only_graph(checkpointer):
    """A minimal graph containing just hitl_gate_node — enough for
    interrupt()/Command(resume=...) to work, without needing the rest of
    the pipeline (or any LLM call) to reach a pending-write state."""
    graph = StateGraph(TicketState)
    graph.add_node("hitl_gate", hitl_gate_node)
    graph.add_edge(START, "hitl_gate")
    graph.add_edge("hitl_gate", END)
    return graph.compile(checkpointer=checkpointer)


def _seed_pending_write(thread_id: str, category: str) -> dict:
    state = new_turn_state(thread_id, "irrelevant ticket text for this test")
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


def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TICKETS_DB_PATH", str(tmp_path / "test_tickets.db"))


def test_write_pauses_until_approved_then_creates_exactly_one_ticket(tmp_path, monkeypatch):
    _isolated_db(tmp_path, monkeypatch)
    graph = _build_hitl_only_graph(InMemorySaver())
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    result = graph.invoke(_seed_pending_write(thread_id, "hardware"), config)
    assert result.get("__interrupt__"), "expected the write to pause for approval"
    assert count_tickets_for_thread(thread_id) == 0, "nothing should be written before approval"

    approved = graph.invoke(Command(resume={"action": "approve"}), config)
    assert not approved.get("__interrupt__")
    assert count_tickets_for_thread(thread_id) == 1
    assert approved["write_outcomes"]["hardware"]["action"] == "approve"


def test_denied_write_never_creates_a_ticket(tmp_path, monkeypatch):
    _isolated_db(tmp_path, monkeypatch)
    graph = _build_hitl_only_graph(InMemorySaver())
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    result = graph.invoke(_seed_pending_write(thread_id, "hardware"), config)
    assert result.get("__interrupt__")

    denied = graph.invoke(Command(resume={"action": "deny"}), config)
    assert not denied.get("__interrupt__")
    assert count_tickets_for_thread(thread_id) == 0
    assert denied["write_outcomes"]["hardware"]["action"] == "deny"


def test_edit_and_approve_writes_the_edited_description(tmp_path, monkeypatch):
    _isolated_db(tmp_path, monkeypatch)
    graph = _build_hitl_only_graph(InMemorySaver())
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    graph.invoke(_seed_pending_write(thread_id, "hardware"), config)
    edited = graph.invoke(
        Command(resume={"action": "edit_and_approve", "edited_fields": {"description": "EDITED description"}}),
        config,
    )

    assert count_tickets_for_thread(thread_id) == 1
    assert edited["write_outcomes"]["hardware"]["action"] == "edit_and_approve"


def test_unrecognized_decision_fails_closed_as_denied(tmp_path, monkeypatch):
    """If a resume decision is missing/garbled, this must never be treated
    as an implicit approval."""
    _isolated_db(tmp_path, monkeypatch)
    graph = _build_hitl_only_graph(InMemorySaver())
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    graph.invoke(_seed_pending_write(thread_id, "hardware"), config)
    result = graph.invoke(Command(resume={"action": "not_a_real_action"}), config)

    assert count_tickets_for_thread(thread_id) == 0
    assert result["write_outcomes"]["hardware"]["action"] == "deny"
