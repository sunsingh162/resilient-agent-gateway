"""Phase 4 Step 4: async job queue — /analyze/async + /jobs/{id}.

Every test points JOBS_DB_PATH at a tmp file and swaps main._analyze_turn
for a canned turn, so no API key is needed. The live checklist (submit,
disconnect, poll later; result survives a server restart) is in the
README's Step 4 section.
"""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import job_queue, job_store, main

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def _isolated_jobs_db(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBS_DB_PATH", str(tmp_path / "jobs.db"))


def _canned_turn(status="completed", response="Rejoin CampusSecure.", gate: threading.Event | None = None):
    def turn(thread_id: str, message: str, endpoint: str = "/analyze") -> main.AnalyzeResponse:
        if gate is not None:
            gate.wait(timeout=5)
        return main.AnalyzeResponse(
            thread_id=thread_id,
            status=status,
            category="network",
            dispatch_categories=["network"],
            response=response,
            pending_writes=[],
            escalated=False,
            latency_ms=42,
        )

    return turn


def _poll_until_terminal(job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/jobs/{job_id}").json()
        if body["status"] in job_store.TERMINAL_STATUSES:
            return body
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never finished")


def test_submit_returns_202_with_job_id_before_the_turn_finishes(monkeypatch):
    gate = threading.Event()  # holds the turn open until we've checked the 202
    monkeypatch.setattr(main, "_analyze_turn", _canned_turn(gate=gate))

    resp = client.post("/analyze/async", json={"message": "wifi drops"})

    assert resp.status_code == 202
    accepted = resp.json()
    assert accepted["job_id"].startswith("job-")
    assert accepted["poll_url"] == f"/jobs/{accepted['job_id']}"
    assert client.get(accepted["poll_url"]).json()["status"] in ("queued", "running")

    gate.set()
    done = _poll_until_terminal(accepted["job_id"])
    assert done["status"] == "completed"
    assert done["result"]["response"] == "Rejoin CampusSecure."
    assert done["started_at"] and done["finished_at"]


def test_result_is_recoverable_from_the_store_without_the_submitting_client(monkeypatch):
    monkeypatch.setattr(main, "_analyze_turn", _canned_turn())

    job_id = client.post("/analyze/async", json={"message": "wifi drops", "thread_id": "t-9"}).json()["job_id"]
    _poll_until_terminal(job_id)

    # A fresh read straight from SQLite — no HTTP client, no in-memory state.
    stored = job_store.get_job(job_id)
    assert stored["status"] == "completed"
    assert stored["thread_id"] == "t-9"
    assert stored["result"]["dispatch_categories"] == ["network"]


def test_paused_turn_lands_as_awaiting_approval(monkeypatch):
    monkeypatch.setattr(main, "_analyze_turn", _canned_turn(status="awaiting_approval", response=None))

    job_id = client.post("/analyze/async", json={"message": "account locked"}).json()["job_id"]

    assert _poll_until_terminal(job_id)["status"] == "awaiting_approval"


def test_a_failing_turn_is_recorded_as_failed_with_the_error(monkeypatch):
    def boom(thread_id, message, endpoint="/analyze"):
        raise RuntimeError("anthropic 529 overloaded")

    monkeypatch.setattr(main, "_analyze_turn", boom)

    job_id = client.post("/analyze/async", json={"message": "wifi drops"}).json()["job_id"]
    done = _poll_until_terminal(job_id)

    assert done["status"] == "failed"
    assert done["result"] is None
    assert "anthropic 529 overloaded" in done["error"]


def test_unknown_job_is_404():
    assert client.get("/jobs/job-does-not-exist").status_code == 404


def test_jobs_orphaned_by_a_restart_are_marked_failed():
    queued = job_store.create_job("t-a", "never started")
    running = job_store.create_job("t-b", "mid-run")
    job_store.mark_running(running["job_id"])
    finished = job_store.create_job("t-c", "already done")
    job_store.mark_finished(finished["job_id"], "completed", result={"response": "ok"})

    assert job_store.fail_orphaned_jobs() == 2

    assert job_store.get_job(queued["job_id"])["status"] == "failed"
    assert "restarted" in job_store.get_job(running["job_id"])["error"]
    assert job_store.get_job(finished["job_id"])["status"] == "completed"


def test_list_jobs_is_most_recent_first():
    first = job_store.create_job("t-1", "one")
    time.sleep(0.01)
    second = job_store.create_job("t-2", "two")

    assert [j["job_id"] for j in job_store.list_jobs()] == [second["job_id"], first["job_id"]]


def test_queue_runs_jobs_concurrently():
    # Two jobs that each wait for the other to start: only possible if the
    # pool really runs them in parallel rather than one after another.
    started = [threading.Event(), threading.Event()]

    def work(i):
        def run():
            started[i].set()
            assert started[1 - i].wait(timeout=2), "jobs ran sequentially"
            return {"status": "completed"}

        return run

    _, f0 = job_queue.submit("t-0", "a", work(0))
    _, f1 = job_queue.submit("t-1", "b", work(1))
    f0.result(timeout=5)
    f1.result(timeout=5)
