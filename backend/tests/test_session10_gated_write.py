"""Session 10 unit tests: the severity gate and idempotency guarantee, both
fully deterministic — no LLM call needed. Each test points TICKETS_DB_PATH
at a fresh tmp_path file so these never touch the real project's
backend/data/tickets.db.
"""

import uuid

import pytest

from app.tools.ticket_store import (
    compute_idempotency_key,
    count_tickets_for_thread,
    create_ticket_record,
)
from app.tools.write_ticket import create_ticket, meets_write_threshold


@pytest.fixture(autouse=True)
def isolated_tickets_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TICKETS_DB_PATH", str(tmp_path / "test_tickets.db"))


def test_meets_write_threshold_gates_by_severity():
    assert meets_write_threshold("low") is False
    assert meets_write_threshold("medium") is True
    assert meets_write_threshold("high") is True
    assert meets_write_threshold("critical") is True
    assert meets_write_threshold("not-a-real-severity") is False  # fail closed


def test_create_ticket_tool_refuses_low_severity():
    result = create_ticket.invoke(
        {
            "category": "network",
            "description": "Routine Wi-Fi drop, self-service fix given.",
            "severity": "low",
            "student_id": "",
        }
    )
    assert result["status"] == "refused"


def test_create_ticket_tool_drafts_for_medium_severity_without_writing():
    """As of Session 11, the tool never writes — it only drafts a proposal
    for hitl_gate_node to act on after an explicit approval decision. See
    test_session11_hitl.py for the actual write path."""
    thread_id = str(uuid.uuid4())
    result = create_ticket.invoke(
        {
            "category": "account",
            "description": "Account locked, needs staff unlock.",
            "severity": "medium",
            "student_id": "S1002",
        },
        config={"configurable": {"thread_id": thread_id}},
    )
    assert result["status"] == "pending_approval"
    assert result["draft"]["thread_id"] == thread_id
    assert count_tickets_for_thread(thread_id) == 0  # nothing written yet


def test_submitting_the_same_request_twice_produces_exactly_one_write():
    """The Session 10 checklist item: a retried write must never double-write."""
    thread_id = str(uuid.uuid4())

    first = create_ticket_record(
        thread_id=thread_id,
        category="account",
        description="Account locked, needs staff unlock.",
        severity="medium",
        student_id="S1002",
    )
    second = create_ticket_record(
        thread_id=thread_id,
        category="account",
        description="Account locked, needs staff unlock (retried after a timeout).",
        severity="medium",
        student_id="S1002",
    )

    assert first["ticket_id"] == second["ticket_id"]
    assert first["created"] is True
    assert second["created"] is False
    assert count_tickets_for_thread(thread_id) == 1


def test_different_categories_in_the_same_thread_each_get_their_own_ticket():
    thread_id = str(uuid.uuid4())

    network_ticket = create_ticket_record(thread_id, "network", "Wi-Fi drops", "medium")
    account_ticket = create_ticket_record(thread_id, "account", "Account locked", "medium")

    assert network_ticket["ticket_id"] != account_ticket["ticket_id"]
    assert count_tickets_for_thread(thread_id) == 2


def test_idempotency_key_is_stable_for_same_thread_and_category():
    key_a = compute_idempotency_key("thread-1", "network")
    key_b = compute_idempotency_key("thread-1", "network")
    key_c = compute_idempotency_key("thread-1", "account")
    assert key_a == key_b
    assert key_a != key_c
