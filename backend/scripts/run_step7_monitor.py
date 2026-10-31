"""Phase 4, Step 7 checklist: the monitoring daemon catches synthetic
degradations and fires the *correct* tripwire for each.

Usage: python backend/scripts/run_step7_monitor.py
Requires a running Phoenix and ANTHROPIC_API_KEY (the judge grades the
seeded turns for real — nothing in the judge-score path is faked).

The drill pins the monitor's window to the moment the drill starts and
includes seeded turns, so only the drill's own seeds are evaluated — real
traffic already in Phoenix can't make a phase pass or fail. It also drops
every SLO's min_sample to 1 (a drill has a handful of turns, not a day's
traffic); thresholds are untouched. Each phase injects one degradation,
ticks, and checks the set of newly paged SLOs is exactly the expected one:

    A. healthy baseline          good turn, graded           → pages nothing
    B. answer-quality regression bad turn (leaks PII), graded → judge_safety_violations + judge_score_mean
    C. provider failure          turn that ended in error    → turn_error_rate
    D. latency regression        turn that took 45s          → turn_latency_p95

Paging state and alerts for the drill go to backend/data/drill_*, so the
drill never opens or resolves a real incident.
"""

import dataclasses
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import judge, monitoring, phoenix_client, tracing  # noqa: E402
from app.paths import resolve_data_path  # noqa: E402
from app.seeded_traces import emit_seeded_turn  # noqa: E402

STATE_PATH = resolve_data_path("backend/data/drill_monitor_state.json")
ALERTS_PATH = resolve_data_path("backend/data/drill_alerts.jsonl")


def _seed(kind: str, label: str, *, status: str = "completed", latency_ms: int, grade: bool) -> None:
    trace_id = emit_seeded_turn(kind, label=label, status=status, latency_ms=latency_ms)
    tracing.flush_tracing()
    deadline = time.monotonic() + 20
    while not (turn_spans := phoenix_client.get_spans(name="helpdesk.turn", trace_id=trace_id)):
        if time.monotonic() > deadline:
            raise SystemExit(f"seeded trace {trace_id} never reached Phoenix — is it running on :6006?")
        time.sleep(0.5)
    if grade:
        turn = judge.turn_from_spans(turn_spans[0], phoenix_client.get_spans(trace_id=trace_id, span_kind="TOOL"))
        [(_, verdict)] = judge.run_judge([turn])
        if verdict is None:
            raise SystemExit(f"judge failed on seed {label}")
        print(f"   judge on {label}: overall={verdict['overall']:.2f} safety={verdict['safety']['score']}")


PHASES = [
    (
        "A. healthy baseline",
        lambda: _seed("good", "drill-good", latency_ms=4_000, grade=True),
        set(),
    ),
    (
        "B. answer-quality regression (reply leaks PII, fakes an account unlock)",
        lambda: _seed("bad", "drill-bad", latency_ms=5_000, grade=True),
        {"judge_safety_violations", "judge_score_mean"},
    ),
    (
        "C. provider failure (turn ended in error)",
        lambda: _seed("good", "drill-error", status="error", latency_ms=1_200, grade=False),
        {"turn_error_rate"},
    ),
    (
        "D. latency regression (turn took 45s)",
        lambda: _seed("good", "drill-slow", latency_ms=45_000, grade=False),
        {"turn_latency_p95"},
    ),
]


def main() -> None:
    if tracing.setup_tracing() is None:
        raise SystemExit("tracing is disabled (TRACING_ENABLED=false) — the drill's seeds need it to reach Phoenix")
    for path in (STATE_PATH, ALERTS_PATH):
        if os.path.exists(path):
            os.remove(path)

    drill_start = datetime.now(timezone.utc).isoformat()
    monitor = monitoring.Monitor(
        include_seeded=True,
        window_start=drill_start,
        slos=[dataclasses.replace(slo, min_sample=1) for slo in monitoring.SLOS],
        state_path=STATE_PATH,
        alerts_path=ALERTS_PATH,
    )

    results = []
    for title, inject, expected in PHASES:
        print(f"\n=== {title}")
        inject()
        report = monitor.tick()
        print(monitoring.format_report(report))
        ok = set(report.paged) == expected
        results.append((title, expected, set(report.paged), ok))

    print("\n=== Step 7 drill summary")
    for title, expected, paged, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {title}")
        print(f"        expected new pages: {sorted(expected) or 'none'}   got: {sorted(paged) or 'none'}")
    passed = all(ok for *_, ok in results)
    print(f"\nStep 7 checklist: {'PASS' if passed else 'FAIL'} — every synthetic degradation fired exactly its own tripwire")
    print(f"Alert log: {ALERTS_PATH}")
    tracing.shutdown_tracing()
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
