"""Phase 4, Step 11: the cut-cable drill.

Cuts the primary tier mid-answer on a live gateway, over a real HTTP
connection, and proves the student's stream survives: one connection,
chunks from two different tiers, a clean `answer_done` at the end. Then
proves the breaker and the monitoring tripwires react the way they should.

Usage (two terminals, Phoenix already running — see README):
    CHAOS_ENABLED=true uvicorn app.main:app --port 8000      # from backend/
    python scripts/run_step11_cut_cable.py [--runs 3] [--cut-after 12]

Phases:
    1. cut runs (×--runs)   primary cut after N chunks → secondary continues
                            in the same connection; the 3rd consecutive cut
                            opens the primary circuit
    2. circuit open         a normal request skips primary entirely
    3. recovery             after the cool-down, a normal request is the
                            half-open trial: primary serves, circuit closes
    4. tripwires            a monitor tick over the drill window pages
                            cascade_fallback_rate + cascade_primary_circuit_open,
                            and nothing else
"""

import argparse
import dataclasses
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import monitoring, phoenix_client  # noqa: E402
from app.fallback_cascade import RECOVERY_SECONDS  # noqa: E402
from app.paths import resolve_data_path  # noqa: E402

GATEWAY = "http://localhost:8000"
QUESTION = "My laptop won't connect to the CampusSecure Wi-Fi in the library. What should I try?"


@dataclasses.dataclass
class StreamResult:
    events: list[dict]
    arrivals: list[float]
    done: dict | None

    def segments(self) -> list[tuple[str, str, str]]:
        segs: list[tuple[str, str, str]] = []
        for e in self.events:
            if e["type"] != "answer_delta":
                continue
            if segs and segs[-1][0] == e["tier"]:
                segs[-1] = (e["tier"], e["model"], segs[-1][2] + e["text"])
            else:
                segs.append((e["tier"], e["model"], e["text"]))
        return segs


def stream(client: httpx.Client, cut_after: int | None) -> StreamResult:
    headers = {"X-Chaos-Cut-Primary-After": str(cut_after)} if cut_after is not None else {}
    events, arrivals = [], []
    start = time.monotonic()
    # One request → one response object → one connection for the whole answer.
    with client.stream("POST", f"{GATEWAY}/answer/stream", json={"question": QUESTION}, headers=headers) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
                arrivals.append(time.monotonic() - start)
    done = next((e for e in events if e["type"] == "answer_done"), None)
    return StreamResult(events, arrivals, done=done)


def print_transcript(result: StreamResult) -> None:
    for e, t in zip(result.events, result.arrivals):
        if e["type"] == "tier_switch":
            print(f"   {t:5.2f}s  ── TIER SWITCH {e['from_tier']} → {e['to_tier']} after {e['chars_already_sent']} chars ({e['reason']})")
        elif e["type"] == "tier_skipped":
            print(f"   {t:5.2f}s  ── TIER SKIPPED {e['tier']} ({e['reason']})")
    for tier, model, text in result.segments():
        print(f"   [{tier} · {model}] {len(text)} chars: {text.strip()[:160]!r}{'…' if len(text) > 160 else ''}")
    if len(result.segments()) > 1:
        a, b = result.segments()[0][2], result.segments()[1][2]
        print(f"   seam: …{a[-40:]!r} ‖ {b[:40]!r}…")
    print(f"   done: {result.done}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--cut-after", type=int, default=12)
    args = parser.parse_args()

    checks: list[tuple[str, bool]] = []
    with httpx.Client(timeout=120) as client:
        health = client.get(f"{GATEWAY}/health").json()
        reset = client.post(f"{GATEWAY}/cascade/reset")
        if reset.status_code == 403:
            raise SystemExit("gateway is not running with CHAOS_ENABLED=true — the drill can't inject a fault")
        print(f"gateway {health}; cascade reset; cutting primary after {args.cut_after} chunks\n")
        drill_start = datetime.now(timezone.utc).isoformat()

        # Phase 1 — repeated cuts
        for run in range(1, args.runs + 1):
            result = stream(client, args.cut_after)
            segments = result.segments()
            print(f"=== Phase 1 · cut run {run}/{args.runs}  ({result.arrivals[-1]:.1f}s, {len(result.events)} events, 1 connection)")
            print_transcript(result)
            ok = (
                [tier for tier, *_ in segments] == ["primary", "secondary"]
                and result.done is not None
                and result.done["status"] == "completed"
                and any(e["type"] == "tier_switch" for e in result.events)
            )
            checks.append((f"cut run {run}: primary then secondary chunks in one stream, clean answer_done", ok))
            print()
        primary_after_cuts = result.done["circuits"]["primary"]
        checks.append((f"{args.runs} consecutive cuts opened the primary circuit", primary_after_cuts == "open"))

        # Phase 2 — circuit open: primary skipped without a request
        before = client.get(f"{GATEWAY}/cascade/health").json()["primary"]["attempts"]
        result = stream(client, cut_after=None)
        after = client.get(f"{GATEWAY}/cascade/health").json()["primary"]["attempts"]
        print(f"=== Phase 2 · circuit open, no fault injected  ({result.arrivals[-1]:.1f}s)")
        print_transcript(result)
        checks.append(
            (
                "open circuit: primary skipped with zero requests, secondary served the whole answer",
                after == before and [t for t, *_ in result.segments()] == ["secondary"]
                and any(e["type"] == "tier_skipped" and e["tier"] == "primary" for e in result.events),
            )
        )

        # Phase 3 — recovery via the half-open trial
        wait = RECOVERY_SECONDS + 1
        print(f"\n=== Phase 3 · waiting {wait:.0f}s for the circuit cool-down, then a normal request")
        time.sleep(wait)
        result = stream(client, cut_after=None)
        print_transcript(result)
        checks.append(
            (
                "recovery: half-open trial succeeded, primary served, circuit closed",
                [t for t, *_ in result.segments()] == ["primary"] and result.done["circuits"]["primary"] == "closed",
            )
        )

    # Phase 4 — tripwires
    print("\n=== Phase 4 · monitoring tick over the drill window (waiting for spans to reach Phoenix)")
    expected_spans = args.runs + 2
    deadline = time.monotonic() + 30
    while len(phoenix_client.get_spans(name="cascade.answer", start_time=drill_start)) < expected_spans:
        if time.monotonic() > deadline:
            raise SystemExit("cascade spans never reached Phoenix — is the gateway exporting to :6006?")
        time.sleep(1)
    monitor = monitoring.Monitor(
        include_seeded=True,
        window_start=drill_start,
        slos=[dataclasses.replace(slo, min_sample=1) for slo in monitoring.SLOS],
        state_path=resolve_data_path("backend/data/drill_cascade_state.json"),
        alerts_path=resolve_data_path("backend/data/drill_cascade_alerts.jsonl"),
    )
    Path(monitor.state_path).unlink(missing_ok=True)
    report = monitor.tick()
    print(monitoring.format_report(report))
    expected_pages = {"cascade_fallback_rate", "cascade_primary_circuit_open"}
    checks.append((f"tripwires: paged exactly {sorted(expected_pages)}", set(report.paged) == expected_pages))

    print("\n=== Step 11 drill summary")
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    passed = all(ok for _, ok in checks)
    print(f"\nStep 11 checklist: {'PASS' if passed else 'FAIL'} — the stream survived a mid-response provider cut, every time")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
