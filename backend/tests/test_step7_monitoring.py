"""Phase 4 Step 7: monitoring daemon — SLO evaluation and incident paging.

collect_snapshot is swapped for canned snapshots, so these run with no
Phoenix and no API key. They pin the SRE behaviour: each SLO measures the
right thing, small samples report NO_DATA instead of judging, a breach
pages exactly once and stays open until it recovers, and incident state
survives a daemon restart. The live drill (real seeds, real judge) is
scripts/run_step7_monitor.py — README Step 7 section.
"""

import dataclasses
import json

import pytest

from app import monitoring


def _turn(status="completed", latency_ms=8000, seeded=None):
    return {"span_id": "s", "status": status, "latency_ms": latency_ms, "seeded": seeded}


def _grade(overall=5.0, safety=5, passed=True, thread_id="t"):
    return {"span_id": "g", "thread_id": thread_id, "overall": overall, "safety": safety, "passed": passed}


def _snap(turns=(), grades=(), jobs=()):
    return monitoring.Snapshot(window_start="2026-10-31T00:00:00+00:00", turns=list(turns), grades=list(grades), jobs=list(jobs))


def _by_name(observations):
    return {o.slo: o for o in observations}


DRILL_SLOS = [dataclasses.replace(slo, min_sample=1) for slo in monitoring.SLOS]


def test_healthy_traffic_is_all_ok():
    snap = _snap(turns=[_turn()] * 5, grades=[_grade()] * 5, jobs=[{"status": "completed"}] * 5)

    assert {o.state for o in monitoring.evaluate(snap)} == {"OK"}


def test_each_slo_measures_its_own_signal():
    snap = _snap(
        turns=[_turn(latency_ms=ms) for ms in (5_000, 6_000, 7_000, 8_000, 60_000)] + [_turn(status="error", latency_ms=None)],
        grades=[_grade(4.0), _grade(4.0), _grade(4.0), _grade(4.0), _grade(1.0, safety=1, passed=False, thread_id="leaky")],
        jobs=[{"status": "completed"}] * 4 + [{"status": "failed"}] * 2 + [{"status": "running"}],
    )

    obs = _by_name(monitoring.evaluate(snap))

    assert obs["turn_latency_p95"].sample == 5  # the errored turn has no latency to count
    assert obs["turn_latency_p95"].observed == pytest.approx(49_600)  # p95 of 5..8s plus one 60s outlier
    assert obs["turn_latency_p95"].state == "BREACH"
    assert obs["turn_error_rate"].observed == pytest.approx(1 / 6)
    assert obs["judge_score_mean"].observed == pytest.approx(3.4)
    assert obs["judge_safety_violations"].observed == 1
    assert obs["judge_safety_violations"].detail == "leaky"
    assert obs["async_job_failure_rate"].sample == 6  # still-running jobs aren't counted yet
    assert obs["async_job_failure_rate"].observed == pytest.approx(2 / 6)


def test_small_samples_report_no_data_instead_of_judging():
    snap = _snap(turns=[_turn(status="error")], grades=[_grade(1.0, safety=1, passed=False)])

    obs = _by_name(monitoring.evaluate(snap))

    assert obs["turn_error_rate"].state == "NO_DATA"  # 1 of 1 errored, but n < 5
    assert obs["judge_score_mean"].state == "NO_DATA"
    assert obs["judge_safety_violations"].state == "BREACH"  # zero tolerance: judges from the first sample


@pytest.fixture
def monitor(tmp_path):
    return monitoring.Monitor(
        slos=DRILL_SLOS,
        state_path=str(tmp_path / "state.json"),
        alerts_path=str(tmp_path / "alerts.jsonl"),
    )


def _feed(monkeypatch, snap):
    monkeypatch.setattr(monitoring, "collect_snapshot", lambda window_start, include_seeded=False: snap)


def _alerts(monitor):
    with open(monitor.alerts_path) as f:
        return [json.loads(line) for line in f]


def test_breach_pages_once_then_stays_quiet_until_it_resolves(monkeypatch, monitor):
    errored = _snap(turns=[_turn(status="error"), _turn()])
    healthy = _snap(turns=[_turn(), _turn()])

    _feed(monkeypatch, errored)
    first = monitor.tick()
    second = monitor.tick()
    _feed(monkeypatch, healthy)
    third = monitor.tick()

    assert first.paged == ["turn_error_rate"]
    assert second.paged == [] and second.breaches == ["turn_error_rate"]  # still breached, not re-paged
    assert third.resolved == ["turn_error_rate"]
    assert [(a["event"], a["slo"]) for a in _alerts(monitor)] == [
        ("page", "turn_error_rate"),
        ("resolved", "turn_error_rate"),
    ]


def test_open_incidents_survive_a_daemon_restart(monkeypatch, monitor):
    _feed(monkeypatch, _snap(turns=[_turn(status="error")]))
    monitor.tick()

    restarted = monitoring.Monitor(slos=DRILL_SLOS, state_path=monitor.state_path, alerts_path=monitor.alerts_path)

    assert restarted.tick().paged == []  # same incident, already paged before the restart


def test_no_data_neither_opens_nor_closes_an_incident(monkeypatch, monitor):
    _feed(monkeypatch, _snap(turns=[_turn(status="error")]))
    monitor.tick()
    _feed(monkeypatch, _snap())
    report = monitor.tick()

    assert report.resolved == []
    assert json.load(open(monitor.state_path))["open_incidents"].keys() == {"turn_error_rate"}


def test_ticket_severity_slo_raises_a_ticket_not_a_page(monkeypatch, monitor):
    _feed(monkeypatch, _snap(jobs=[{"status": "failed"}]))

    monitor.tick()

    assert [(a["event"], a["slo"]) for a in _alerts(monitor)] == [("ticket", "async_job_failure_rate")]


def test_pager_webhook_receives_the_alert(monkeypatch, monitor):
    posted = []
    monkeypatch.setenv("PAGER_WEBHOOK_URL", "https://hooks.example/pager")
    monkeypatch.setattr(monitoring.httpx, "post", lambda url, json, timeout: posted.append((url, json)))
    _feed(monkeypatch, _snap(grades=[_grade(1.0, safety=1, passed=False, thread_id="leaky")]))

    monitor.tick()

    urls = {url for url, _ in posted}
    slos = {body["event"]["slo"] for _, body in posted}
    assert urls == {"https://hooks.example/pager"}
    assert slos == {"judge_safety_violations", "judge_score_mean"}


def test_seeded_turns_are_excluded_from_real_traffic(monkeypatch):
    spans = [
        {"context": {"span_id": "real"}, "attributes": {"helpdesk.status": "completed", "helpdesk.latency_ms": 9000}},
        {"context": {"span_id": "seed"}, "attributes": {"helpdesk.status": "error", "helpdesk.seeded": "drill-error"}},
    ]
    monkeypatch.setattr(monitoring.phoenix_client, "get_spans", lambda **kwargs: spans)
    monkeypatch.setattr(monitoring.judge_store, "recent_grades", lambda limit, since: [])
    monkeypatch.setattr(monitoring.job_store, "list_jobs", lambda limit, since: [])

    assert [t["span_id"] for t in monitoring.collect_snapshot("x").turns] == ["real"]
    assert [t["span_id"] for t in monitoring.collect_snapshot("x", include_seeded=True).turns] == ["real", "seed"]
