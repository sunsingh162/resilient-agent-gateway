"""Phase 4 Step 6: LLM-as-judge pipeline.

The judge LLM and Phoenix's REST API are both faked here, so these run
with no API key and no Phoenix process. They pin the pipeline around the
model call: turning Phoenix spans into a gradable turn, the pass rule,
idempotent grading, the Phoenix annotations written back, and the shape
of the seeded traces. The live good-vs-bad grading is
scripts/run_step6_judge.py (README Step 6 section).
"""

import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import judge, judge_store, seeded_traces


@pytest.fixture(autouse=True)
def _isolated_judge_db(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDGE_DB_PATH", str(tmp_path / "judge.db"))


def _turn_span(span_id, status="completed", seeded=None, thread_id="t-1"):
    attrs = {
        "helpdesk.thread_id": thread_id,
        "helpdesk.endpoint": "/analyze",
        "helpdesk.status": status,
        "input.value": "wifi drops",
        "output.value": "Rejoin CampusSecure.",
    }
    if seeded:
        attrs["helpdesk.seeded"] = seeded
    return {"context": {"span_id": span_id, "trace_id": f"trace-{span_id}"}, "attributes": attrs, "start_time": "2026-10-31T00:00:00Z"}


def _tool_span(name, args, output, start):
    return {
        "name": name,
        "start_time": start,
        "attributes": {"tool.name": name, "input.value": args, "output.value": output},
    }


def _verdict(correctness=5, safety=5, tool_use=5):
    return judge.summarize_verdict(
        judge.JudgeVerdict(
            correctness={"score": correctness, "reason": "c"},
            safety={"score": safety, "reason": "s"},
            tool_use={"score": tool_use, "reason": "t"},
        )
    )


def test_turn_from_spans_orders_tool_calls_and_unwraps_toolmessage_output():
    wrapped = json.dumps({"type": "tool", "data": {"content": '{"account_locked": true}', "tool_call_id": "toolu_1"}})
    tools = [  # Phoenix returns newest first
        _tool_span("assess_severity", '{"category": "account"}', '{"severity": "medium"}', "2026-10-31T00:00:02Z"),
        _tool_span("lookup_student_account", "S1002", wrapped, "2026-10-31T00:00:01Z"),
    ]

    turn = judge.turn_from_spans(_turn_span("s1"), tools)

    assert [c["tool"] for c in turn.tool_calls] == ["lookup_student_account", "assess_severity"]
    assert turn.tool_calls[0]["result"] == '{"account_locked": true}'
    assert turn.tool_calls[1]["result"] == '{"severity": "medium"}'  # plain JSON passes through
    assert turn.ticket == "wifi drops" and turn.response == "Rejoin CampusSecure."


def test_one_failing_axis_fails_the_turn_even_with_a_high_average():
    verdict = _verdict(correctness=5, safety=1, tool_use=5)

    assert verdict["overall"] == pytest.approx(3.67, abs=0.01)
    assert verdict["passed"] is False


def test_all_axes_at_the_floor_pass():
    assert _verdict(3, 3, 3)["passed"] is True


def test_run_judge_persists_and_annotates_each_axis(monkeypatch):
    posted = []
    monkeypatch.setattr(judge, "grade_turn", lambda turn: _verdict(4, 5, 2))
    monkeypatch.setattr(judge.phoenix_client, "annotate_spans", posted.extend)

    turn = judge.turn_from_spans(_turn_span("s1", seeded="bad"), [])
    [(_, verdict)] = judge.run_judge([turn])

    assert verdict["passed"] is False
    assert {a["name"]: a["result"]["label"] for a in posted} == {
        "judge.correctness": "pass",
        "judge.safety": "pass",
        "judge.tool_use": "fail",
        "judge.overall": "fail",
    }
    assert all(a["span_id"] == "s1" and a["annotator_kind"] == "LLM" for a in posted)
    [stored] = judge_store.recent_grades()
    assert (stored["span_id"], stored["seeded"], stored["tool_use"], stored["passed"]) == ("s1", "bad", 2, False)


def test_a_judge_error_leaves_the_turn_ungraded_without_aborting_the_batch(monkeypatch):
    def flaky(turn):
        if turn.span_id == "s1":
            raise RuntimeError("judge model overloaded")
        return _verdict()

    monkeypatch.setattr(judge, "grade_turn", flaky)
    monkeypatch.setattr(judge.phoenix_client, "annotate_spans", lambda annotations: None)

    results = judge.run_judge([judge.turn_from_spans(_turn_span(sid), []) for sid in ("s1", "s2")])

    assert [(t.span_id, v is not None) for t, v in results] == [("s1", False), ("s2", True)]
    assert [g["span_id"] for g in judge_store.recent_grades()] == ["s2"]


def test_collect_turns_skips_graded_and_ungradable_turns(monkeypatch):
    turn_spans = [
        _turn_span("done-graded"),
        _turn_span("done-new"),
        _turn_span("paused", status="awaiting_approval"),
        _turn_span("errored", status="error"),
        _turn_span("blocked", status="blocked"),
        _turn_span("seed", seeded="bad"),
    ]

    def fake_get_spans(name=None, trace_id=None, span_kind=None, limit=None):
        return turn_spans if name == "helpdesk.turn" else []

    monkeypatch.setattr(judge.phoenix_client, "get_spans", fake_get_spans)
    judge_store.save_grade(judge.turn_from_spans(turn_spans[0], []).as_dict(), _verdict(), "test-model")

    assert [t.span_id for t in judge.collect_turns()] == ["done-new", "paused"]
    assert [t.span_id for t in judge.collect_turns(include_seeded=True)] == ["done-new", "paused", "seed"]


def test_seeded_turns_have_the_live_trace_shape(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(seeded_traces, "get_tracer", lambda: provider.get_tracer("test"))

    good_trace = seeded_traces.emit_seeded_turn("good")
    bad_trace = seeded_traces.emit_seeded_turn("bad")

    spans = exporter.get_finished_spans()
    roots = {s.attributes["helpdesk.seeded"]: s for s in spans if s.name == "helpdesk.turn"}
    assert format(roots["good"].context.trace_id, "032x") == good_trace
    assert format(roots["bad"].context.trace_id, "032x") == bad_trace
    assert roots["good"].attributes["helpdesk.status"] == "completed"

    good_tools = [s for s in spans if s.context.trace_id == roots["good"].context.trace_id and s.name != "helpdesk.turn"]
    assert [s.attributes["tool.name"] for s in good_tools] == ["search_kb", "assess_severity"]
    assert all(s.attributes["openinference.span.kind"] == "TOOL" for s in good_tools)
    assert all(s.parent.span_id == roots["good"].context.span_id for s in good_tools)
    assert not [s for s in spans if s.context.trace_id == roots["bad"].context.trace_id and s.name != "helpdesk.turn"]
