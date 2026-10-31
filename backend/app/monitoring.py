"""Phase 4, Step 7: SRE-style monitoring daemon — named SLOs, tripwires, paging.

Every tick() reads a rolling window from three sources, all populated by
earlier steps:

    Phoenix `helpdesk.turn` spans (Step 5)  → latency, error rate
    judge_store grades (Step 6)             → judge score, safety violations
    job_store jobs (Step 4)                 → async job failure rate

and evaluates each SLO below. A breach pages once, on the OK → BREACH
transition, and stays an open incident (no re-paging every tick) until the
metric recovers, which records a resolution. Incident state survives a
daemon restart (monitor_state.json), so a restart doesn't re-page an
incident someone is already handling.

"Paging" here means: an alert line on stderr, an append to
backend/data/alerts.jsonl, and — if PAGER_WEBHOOK_URL is set — a JSON POST
to it (a Slack incoming webhook or a PagerDuty Events endpoint both accept
one). There's no real on-call rotation behind it; see the README.

Seeded (synthetic) turns are excluded from real-traffic monitoring unless
include_seeded is set — the Step 7 drill sets it, over a window that starts
when the drill does, so only the drill's own seeds are evaluated.
"""

import argparse
import json
import logging
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from . import job_store, judge_store, phoenix_client
from .paths import resolve_data_path

logger = logging.getLogger("monitoring")


@dataclass(frozen=True)
class SLO:
    name: str
    description: str
    # "max": breached when observed > threshold. "min": breached when observed < threshold.
    direction: str
    threshold: float
    unit: str
    # Below this many samples in the window, the SLO reports NO DATA instead
    # of judging — one slow request out of one isn't a p95 regression.
    min_sample: int
    severity: str  # "page" wakes a human; "ticket" goes to the queue for working hours


SLOS = [
    SLO(
        name="turn_latency_p95",
        description="p95 end-to-end latency of a helpdesk turn",
        direction="max",
        threshold=float(os.getenv("SLO_LATENCY_P95_MS", "30000")),
        unit="ms",
        min_sample=int(os.getenv("SLO_MIN_SAMPLE", "5")),
        severity="page",
    ),
    SLO(
        name="turn_error_rate",
        description="share of helpdesk turns that ended in an error",
        direction="max",
        threshold=float(os.getenv("SLO_ERROR_RATE", "0.10")),
        unit="ratio",
        min_sample=int(os.getenv("SLO_MIN_SAMPLE", "5")),
        severity="page",
    ),
    SLO(
        name="judge_score_mean",
        description="mean LLM-judge overall score across graded turns",
        direction="min",
        threshold=float(os.getenv("SLO_JUDGE_MEAN", "3.5")),
        unit="score",
        min_sample=int(os.getenv("SLO_MIN_SAMPLE", "5")),
        severity="page",
    ),
    SLO(
        # Zero-tolerance, so it judges from the first sample: a single reply
        # that leaks PII or invents an account change is an incident on its own.
        name="judge_safety_violations",
        description="graded turns with a judge safety score of 2 or below",
        direction="max",
        threshold=0,
        unit="count",
        min_sample=1,
        severity="page",
    ),
    # ---- Step 11: cascade health, read from `cascade.answer` spans (/answer/stream)
    SLO(
        name="cascade_fallback_rate",
        description="share of streamed answers that needed a tier other than primary",
        direction="max",
        threshold=float(os.getenv("SLO_CASCADE_FALLBACK_RATE", "0.20")),
        unit="ratio",
        min_sample=int(os.getenv("SLO_MIN_SAMPLE", "5")),
        severity="page",
    ),
    SLO(
        # Zero tolerance: an open primary circuit means the main model is down
        # *right now* and every student is on a fallback tier.
        name="cascade_primary_circuit_open",
        description="streamed answers that found the primary tier's circuit open",
        direction="max",
        threshold=0,
        unit="count",
        min_sample=1,
        severity="page",
    ),
    SLO(
        name="cascade_exhausted",
        description="streamed answers where every tier failed",
        direction="max",
        threshold=0,
        unit="count",
        min_sample=1,
        severity="page",
    ),
    SLO(
        name="async_job_failure_rate",
        description="share of async jobs that ended failed",
        direction="max",
        threshold=float(os.getenv("SLO_JOB_FAILURE_RATE", "0.20")),
        unit="ratio",
        min_sample=int(os.getenv("SLO_MIN_SAMPLE", "5")),
        severity="ticket",
    ),
]

SAFETY_VIOLATION_MAX_SCORE = 2


@dataclass
class Snapshot:
    """Everything one tick reads, already windowed and seed-filtered."""

    window_start: str
    turns: list[dict] = field(default_factory=list)
    grades: list[dict] = field(default_factory=list)
    jobs: list[dict] = field(default_factory=list)
    cascades: list[dict] = field(default_factory=list)


@dataclass
class Observation:
    slo: str
    observed: float | None
    threshold: float
    unit: str
    direction: str
    sample: int
    state: str  # OK | BREACH | NO_DATA
    severity: str
    detail: str = ""

    @property
    def target(self) -> str:
        """e.g. "<= 30000 ms" — the SLO as a human reads it."""
        return f"{'<=' if self.direction == 'max' else '>='} {self.threshold:g}"


def _p95(values: list[float]) -> float:
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=20, method="inclusive")[-1]


def collect_snapshot(window_start: str, include_seeded: bool = False) -> Snapshot:
    turns = []
    for span in phoenix_client.get_spans(name="helpdesk.turn", start_time=window_start):
        attrs = span.get("attributes") or {}
        if attrs.get("helpdesk.seeded") and not include_seeded:
            continue
        turns.append(
            {
                "span_id": span["context"]["span_id"],
                "status": attrs.get("helpdesk.status"),
                "latency_ms": attrs.get("helpdesk.latency_ms"),
                "seeded": attrs.get("helpdesk.seeded"),
            }
        )
    grades = [
        g for g in judge_store.recent_grades(limit=1000, since=window_start) if include_seeded or not g.get("seeded")
    ]
    jobs = job_store.list_jobs(limit=1000, since=window_start)
    cascades = []
    for span in phoenix_client.get_spans(name="cascade.answer", start_time=window_start):
        attrs = span.get("attributes") or {}
        # A drill's deliberately injected faults are synthetic, like seeded
        # turns — excluded from real-traffic monitoring unless asked for.
        if attrs.get("cascade.chaos") and not include_seeded:
            continue
        cascades.append(
            {
                "span_id": span["context"]["span_id"],
                "fallback": bool(attrs.get("cascade.fallback")),
                "primary_circuit_at_start": attrs.get("cascade.primary_circuit_at_start"),
                "skipped_open": attrs.get("cascade.skipped_open", 0),
                "exhausted": bool(attrs.get("cascade.exhausted")),
                "chaos": bool(attrs.get("cascade.chaos")),
            }
        )
    return Snapshot(window_start=window_start, turns=turns, grades=grades, jobs=jobs, cascades=cascades)


def _measure(slo: SLO, snap: Snapshot) -> tuple[float | None, int, str]:
    """(observed value, sample size, human detail) for one SLO."""
    if slo.name == "turn_latency_p95":
        latencies = [t["latency_ms"] for t in snap.turns if t["status"] != "error" and t["latency_ms"] is not None]
        return (_p95(latencies) if latencies else None), len(latencies), f"max {max(latencies, default=0)}ms"
    if slo.name == "turn_error_rate":
        errors = sum(t["status"] == "error" for t in snap.turns)
        return (errors / len(snap.turns) if snap.turns else None), len(snap.turns), f"{errors} errored"
    if slo.name == "judge_score_mean":
        scores = [g["overall"] for g in snap.grades]
        failing = sum(not g["passed"] for g in snap.grades)
        return (statistics.mean(scores) if scores else None), len(scores), f"{failing} failed the judge"
    if slo.name == "judge_safety_violations":
        unsafe = [g for g in snap.grades if g["safety"] <= SAFETY_VIOLATION_MAX_SCORE]
        detail = ", ".join(g.get("thread_id") or g["span_id"] for g in unsafe[:3])
        return (float(len(unsafe)) if snap.grades else None), len(snap.grades), detail
    if slo.name == "cascade_fallback_rate":
        fell_back = sum(c["fallback"] for c in snap.cascades)
        return (fell_back / len(snap.cascades) if snap.cascades else None), len(snap.cascades), f"{fell_back} used a fallback tier"
    if slo.name == "cascade_primary_circuit_open":
        found_open = sum(c["primary_circuit_at_start"] == "open" for c in snap.cascades)
        return (float(found_open) if snap.cascades else None), len(snap.cascades), f"{found_open} hit an open primary circuit"
    if slo.name == "cascade_exhausted":
        exhausted = sum(c["exhausted"] for c in snap.cascades)
        return (float(exhausted) if snap.cascades else None), len(snap.cascades), f"{exhausted} with every tier failed"
    if slo.name == "async_job_failure_rate":
        finished = [j for j in snap.jobs if j["status"] in job_store.TERMINAL_STATUSES]
        failed = sum(j["status"] == "failed" for j in finished)
        return (failed / len(finished) if finished else None), len(finished), f"{failed} failed"
    raise ValueError(f"no measurement defined for SLO {slo.name!r}")


def evaluate(snap: Snapshot, slos: list[SLO] = SLOS) -> list[Observation]:
    observations = []
    for slo in slos:
        observed, sample, detail = _measure(slo, snap)
        if observed is None or sample < slo.min_sample:
            state = "NO_DATA"
        elif slo.direction == "max":
            state = "BREACH" if observed > slo.threshold else "OK"
        else:
            state = "BREACH" if observed < slo.threshold else "OK"
        observations.append(
            Observation(slo.name, observed, slo.threshold, slo.unit, slo.direction, sample, state, slo.severity, detail)
        )
    return observations


def _data_path(env_var: str, filename: str) -> str:
    path = resolve_data_path(os.getenv(env_var) or f"backend/data/{filename}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def _load_open_incidents(state_path: str) -> dict:
    try:
        with open(state_path) as f:
            return json.load(f).get("open_incidents", {})
    except FileNotFoundError:
        return {}


def _save_open_incidents(state_path: str, incidents: dict) -> None:
    with open(state_path, "w") as f:
        json.dump({"open_incidents": incidents}, f, indent=2)


def _notify(event: dict, alerts_path: str) -> None:
    with open(alerts_path, "a") as f:
        f.write(json.dumps(event) + "\n")
    banner = "🚨 PAGE" if event["event"] == "page" else ("📋 TICKET" if event["event"] == "ticket" else "✅ RESOLVED")
    print(
        f"{banner} [{event['slo']}] observed={event['observed']} (SLO {event['target']}) — {event['detail']}",
        file=sys.stderr,
    )
    webhook = os.getenv("PAGER_WEBHOOK_URL")
    if webhook:
        try:
            httpx.post(webhook, json={"text": f"{banner} {event['slo']}: {event['detail']}", "event": event}, timeout=5)
        except httpx.HTTPError:
            logger.exception("[monitoring] pager webhook failed — alert is still in %s", alerts_path)


@dataclass
class TickReport:
    at: str
    window_start: str
    observations: list[Observation]
    paged: list[str]
    resolved: list[str]

    @property
    def breaches(self) -> list[str]:
        return [o.slo for o in self.observations if o.state == "BREACH"]


class Monitor:
    def __init__(
        self,
        window: timedelta = timedelta(minutes=60),
        include_seeded: bool = False,
        slos: list[SLO] = SLOS,
        state_path: str | None = None,
        alerts_path: str | None = None,
        window_start: str | None = None,
    ):
        self.window = window
        self.include_seeded = include_seeded
        self.slos = slos
        # A fixed window_start pins the window (the drill uses this so only
        # its own seeds count); otherwise it rolls with each tick.
        self.fixed_window_start = window_start
        self.state_path = state_path or _data_path("MONITOR_STATE_PATH", "monitor_state.json")
        self.alerts_path = alerts_path or _data_path("ALERTS_LOG_PATH", "alerts.jsonl")

    def tick(self) -> TickReport:
        now = datetime.now(timezone.utc)
        window_start = self.fixed_window_start or (now - self.window).isoformat()
        observations = evaluate(collect_snapshot(window_start, self.include_seeded), self.slos)

        incidents = _load_open_incidents(self.state_path)
        paged, resolved = [], []
        for obs in observations:
            event = {
                "at": now.isoformat(),
                "slo": obs.slo,
                "observed": None if obs.observed is None else round(obs.observed, 3),
                "target": obs.target,
                "sample": obs.sample,
                "detail": obs.detail,
                "severity": obs.severity,
            }
            if obs.state == "BREACH" and obs.slo not in incidents:
                incidents[obs.slo] = {"opened_at": now.isoformat(), "severity": obs.severity}
                _notify({**event, "event": "page" if obs.severity == "page" else "ticket"}, self.alerts_path)
                paged.append(obs.slo)
            elif obs.state == "OK" and obs.slo in incidents:
                opened = incidents.pop(obs.slo)
                _notify({**event, "event": "resolved", "opened_at": opened["opened_at"]}, self.alerts_path)
                resolved.append(obs.slo)
            # NO_DATA neither opens nor closes an incident: no evidence either way.
        _save_open_incidents(self.state_path, incidents)
        return TickReport(now.isoformat(), window_start, observations, paged, resolved)


def format_report(report: TickReport) -> str:
    lines = [f"tick @ {report.at[:19]}Z  window since {report.window_start[:19]}Z"]
    lines.append(f"  {'SLO':<26}{'observed':>10}  {'target':<10}{'n':>4}  state")
    for o in report.observations:
        observed = "—" if o.observed is None else (f"{o.observed:.0f}" if o.unit in ("ms", "count") else f"{o.observed:.2f}")
        flag = {"OK": "OK", "BREACH": f"BREACH ({o.severity})", "NO_DATA": "no data"}[o.state]
        lines.append(f"  {o.slo:<26}{observed:>10}  {o.target:<10}{o.sample:>4}  {flag}  {o.detail}")
    if report.paged:
        lines.append(f"  → paged: {', '.join(report.paged)}")
    if report.resolved:
        lines.append(f"  → resolved: {', '.join(report.resolved)}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Helpdesk swarm monitoring daemon")
    parser.add_argument("--once", action="store_true", help="run a single tick and exit")
    parser.add_argument("--interval", type=int, default=60, help="seconds between ticks")
    parser.add_argument("--window-minutes", type=int, default=60)
    parser.add_argument("--include-seeded", action="store_true")
    args = parser.parse_args()

    monitor = Monitor(window=timedelta(minutes=args.window_minutes), include_seeded=args.include_seeded)
    while True:
        report = monitor.tick()
        print(format_report(report), flush=True)
        if args.once:
            sys.exit(1 if report.breaches else 0)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
