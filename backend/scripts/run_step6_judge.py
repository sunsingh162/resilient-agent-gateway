"""Phase 4, Step 6 checklist: the judge gives a seeded good trace and a
seeded bad trace visibly different scores.

Usage:
    python backend/scripts/run_step6_judge.py            # seed good+bad, grade both
    python backend/scripts/run_step6_judge.py --recent 5 # also grade 5 newest ungraded real turns

Requires a running Phoenix (see README) and ANTHROPIC_API_KEY. The seeded
traces go into Phoenix through OpenTelemetry first and are then read back
out through Phoenix's REST API — the judge never sees them any other way.
"""

import argparse
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import judge, phoenix_client, tracing  # noqa: E402
from app.seeded_traces import emit_seeded_turn  # noqa: E402


def _wait_for_turn_span(trace_id: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        spans = phoenix_client.get_spans(name="helpdesk.turn", trace_id=trace_id)
        if spans:
            return spans[0]
        time.sleep(0.5)
    raise SystemExit(f"seeded trace {trace_id} never showed up in Phoenix — is it running on :6006?")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recent", type=int, default=0, help="also grade this many newest ungraded real turns")
    args = parser.parse_args()

    if tracing.setup_tracing() is None:
        raise SystemExit("tracing is disabled (TRACING_ENABLED=false) — the seeds need it to reach Phoenix")

    trace_ids = {kind: emit_seeded_turn(kind) for kind in ("good", "bad")}
    tracing.shutdown_tracing()  # flush the seeded spans to Phoenix now
    print(f"seeded into Phoenix: {trace_ids}\n")

    seeded_turns = {}
    for kind, trace_id in trace_ids.items():
        turn_span = _wait_for_turn_span(trace_id)
        tool_spans = phoenix_client.get_spans(trace_id=trace_id, span_kind="TOOL")
        seeded_turns[kind] = judge.turn_from_spans(turn_span, tool_spans)

    print(f"judge model: {judge.JUDGE_MODEL}   pass floor: every axis >= {judge.PASS_FLOOR}\n")
    graded = dict(zip(seeded_turns, (verdict for _, verdict in judge.run_judge(list(seeded_turns.values())))))
    for kind, verdict in graded.items():
        if verdict is None:
            raise SystemExit(f"judge failed on the {kind} seed — see the log above")
        print(judge.format_verdict(seeded_turns[kind], verdict), "\n")

    if args.recent:
        print(f"--- {args.recent} newest ungraded real turns ---")
        for turn, verdict in judge.run_judge(judge.collect_turns(limit=args.recent)):
            print(judge.format_verdict(turn, verdict) if verdict else f"{turn.thread_id}: judge failed", "\n")

    good, bad = graded["good"], graded["bad"]
    gap = good["overall"] - bad["overall"]
    passed = good["passed"] and not bad["passed"] and gap >= 2
    print(
        f"Step 6 checklist: {'PASS' if passed else 'FAIL'} — good {good['overall']:.2f} "
        f"({'pass' if good['passed'] else 'fail'}) vs bad {bad['overall']:.2f} "
        f"({'pass' if bad['passed'] else 'fail'}), gap {gap:.2f}"
    )
    print("Scores are also on each trace in Phoenix: http://localhost:6006 → resilient-agent-gateway → Annotations")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
