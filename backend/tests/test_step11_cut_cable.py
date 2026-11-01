"""Phase 4 Step 11: /answer/stream, chaos gating, cascade tripwires.

The shared cascade is swapped for one built on fake providers, and spans go
to an in-memory exporter — no API key, no Phoenix. These pin the HTTP
contract of the drill: the stream survives a cut in one response, breaker
state persists across requests, chaos is refused unless enabled, and the
monitoring daemon measures the cascade.answer spans correctly. The live
drill is scripts/run_step11_cut_cable.py (README Step 11 section).
"""

import json

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import answer_stream, main, monitoring
from app import fallback_cascade as fc

client = TestClient(main.app)


def fake(chunks):
    def stream(messages):
        yield from chunks

    return stream


@pytest.fixture
def cascade(monkeypatch):
    c = fc.FallbackCascade(
        providers={
            "primary": fake(["Forget ", "and ", "rejoin ", "CampusSecure."]),
            "secondary": fake(["rejoin CampusSecure, then restart."]),
            "tertiary": fake(["KB-102"]),
        },
        models={"primary": "model-a", "secondary": "model-b", "tertiary": "local"},
    )
    monkeypatch.setattr(answer_stream, "get_cascade", lambda: c)
    return c


@pytest.fixture
def chaos(monkeypatch):
    monkeypatch.setenv("CHAOS_ENABLED", "true")


@pytest.fixture
def spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(answer_stream, "get_tracer", lambda: provider.get_tracer("test"))
    return exporter


def _ask(cut_after=None):
    headers = {"X-Chaos-Cut-Primary-After": str(cut_after)} if cut_after is not None else {}
    resp = client.post("/answer/stream", json={"question": "wifi drops"}, headers=headers)
    return resp, [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]


# ---- chaos gating -----------------------------------------------------------


def test_fault_injection_is_refused_unless_the_gateway_enables_chaos(cascade, monkeypatch):
    monkeypatch.delenv("CHAOS_ENABLED", raising=False)

    resp, _ = _ask(cut_after=2)

    assert resp.status_code == 403
    assert cascade.health_report()["primary"]["attempts"] == 0  # nothing ran


def test_cascade_reset_is_refused_unless_chaos_is_enabled(monkeypatch):
    monkeypatch.delenv("CHAOS_ENABLED", raising=False)

    assert client.post("/cascade/reset").status_code == 403


def test_a_normal_request_needs_no_chaos_flag(cascade, monkeypatch):
    monkeypatch.delenv("CHAOS_ENABLED", raising=False)

    resp, events = _ask()

    assert resp.status_code == 200
    assert events[-1]["served_by"] == ["primary"]


# ---- the stream survives a cut ----------------------------------------------


def test_cut_primary_hands_over_to_secondary_in_the_same_response(cascade, chaos, spans):
    resp, events = _ask(cut_after=2)

    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
    types = [e["type"] for e in events]
    assert types[0] == "run_started" and types[-1] == "answer_done"
    deltas = [(e["tier"], e["text"]) for e in events if e["type"] == "answer_delta"]
    # Released at word boundaries: the last word is flushed when the tier finishes.
    assert deltas == [
        ("primary", "Forget "),
        ("primary", "and "),
        ("secondary", "rejoin CampusSecure, then "),
        ("secondary", "restart."),
    ]
    [switch] = [e for e in events if e["type"] == "tier_switch"]
    assert (switch["from_tier"], switch["to_tier"], switch["chars_already_sent"]) == ("primary", "secondary", len("Forget and "))
    assert events[-1] == {
        **events[-1],
        "status": "completed",
        "served_by": ["primary", "secondary"],
        "chars_by_tier": {"primary": 11, "secondary": 34, "tertiary": 0},
    }


def test_breaker_state_carries_across_requests(cascade, chaos, spans):
    for _ in range(3):
        _ask(cut_after=1)

    _, events = _ask()  # no fault this time

    assert any(e["type"] == "tier_skipped" and e["tier"] == "primary" for e in events)
    assert events[-1]["served_by"] == ["secondary"]
    assert events[-1]["circuits"]["primary"] == "open"
    assert client.get("/cascade/health").json()["primary"]["skipped_open"] == 1


def test_every_answer_is_traced_with_the_attributes_the_tripwires_read(cascade, chaos, spans):
    _ask(cut_after=2)

    [span] = [s for s in spans.get_finished_spans() if s.name == "cascade.answer"]
    attrs = dict(span.attributes)
    assert attrs["cascade.chaos"] is True
    assert tuple(attrs["cascade.served_by"]) == ("primary", "secondary")
    assert attrs["cascade.fallback"] is True
    assert attrs["cascade.switches"] == 1
    assert attrs["cascade.exhausted"] is False
    assert attrs["cascade.primary_circuit_at_start"] == "closed"
    assert attrs["cascade.circuit.primary"] == "closed"
    assert attrs["output.value"] == "Forget and rejoin CampusSecure, then restart."
    assert [e.name for e in span.events] == ["cascade.tier_switch"]


def test_exhausted_cascade_still_ends_the_stream_with_answer_done(chaos, spans, monkeypatch):
    def down(messages):
        raise fc.ProviderFailure("down")
        yield  # pragma: no cover — makes this a generator

    c = fc.FallbackCascade(providers={p: down for p in fc.TIER_ORDER}, models={p: p for p in fc.TIER_ORDER})
    monkeypatch.setattr(answer_stream, "get_cascade", lambda: c)

    _, events = _ask()

    assert events[-1]["type"] == "answer_done" and events[-1]["status"] == "exhausted"
    assert [s for s in spans.get_finished_spans() if s.name == "cascade.answer"][0].attributes["cascade.exhausted"] is True


# ---- monitoring tripwires ---------------------------------------------------


def _cascade_row(fallback=False, circuit="closed", exhausted=False):
    return {"span_id": "c", "fallback": fallback, "primary_circuit_at_start": circuit, "skipped_open": 0, "exhausted": exhausted, "chaos": True}


def test_cascade_slos_measure_fallback_rate_open_circuits_and_exhaustion():
    snap = monitoring.Snapshot(
        window_start="x",
        cascades=[_cascade_row(fallback=True)] * 3 + [_cascade_row(fallback=True, circuit="open")] + [_cascade_row()],
    )

    obs = {o.slo: o for o in monitoring.evaluate(snap)}

    assert obs["cascade_fallback_rate"].observed == pytest.approx(0.8)
    assert obs["cascade_fallback_rate"].state == "BREACH"
    assert obs["cascade_primary_circuit_open"].observed == 1 and obs["cascade_primary_circuit_open"].state == "BREACH"
    assert obs["cascade_exhausted"].observed == 0 and obs["cascade_exhausted"].state == "OK"


def test_chaos_runs_are_excluded_from_real_traffic_monitoring(monkeypatch):
    def fake_spans(name=None, start_time=None, **kwargs):
        if name != "cascade.answer":
            return []
        return [
            {"context": {"span_id": "real"}, "attributes": {"cascade.fallback": False, "cascade.primary_circuit_at_start": "closed"}},
            {"context": {"span_id": "drill"}, "attributes": {"cascade.fallback": True, "cascade.chaos": True}},
        ]

    monkeypatch.setattr(monitoring.phoenix_client, "get_spans", fake_spans)
    monkeypatch.setattr(monitoring.judge_store, "recent_grades", lambda limit, since: [])
    monkeypatch.setattr(monitoring.job_store, "list_jobs", lambda limit, since: [])

    assert [c["span_id"] for c in monitoring.collect_snapshot("x").cascades] == ["real"]
    assert [c["span_id"] for c in monitoring.collect_snapshot("x", include_seeded=True).cascades] == ["real", "drill"]


# ---- reviewer demo page -----------------------------------------------------


def test_demo_page_is_served_and_wired_to_the_real_endpoints():
    resp = client.get("/demo")

    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/html")
    page = resp.text
    for endpoint in ("/answer/stream", "/analyze/stream", '"/analyze"', "/cache/stats", "/cascade/reset", "X-Chaos-Cut-Primary-After"):
        assert endpoint in page
