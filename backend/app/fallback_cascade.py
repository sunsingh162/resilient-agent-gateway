"""Phase 4, Step 10: fallback cascade + circuit breaker + mid-stream hot-swap.

A streamed answer is served by the first healthy tier in a fixed order:

    primary    claude-sonnet-5       (the standard model the swarm already uses)
    secondary  claude-haiku-4-5      (a different model, so one model's outage
                                      or overload doesn't take out both)
    tertiary   local KB responder    (no network, no API key: answers from the
                                      helpdesk knowledge base, so a student still
                                      gets the right article when every model is down)

Everything — order, breakers, health counters, the per-chunk attribution — is
keyed by tier *position*, never by model string. Two positions may share a
model (TIER_SECONDARY_MODEL=claude-sonnet-5 is a legitimate config, e.g. a
second region) and they still keep separate breakers and separate counts; a
model-keyed dict would silently merge them and the report could no longer say
*which* tier caught a failure.

Circuit breaker, one per position:

    CLOSED     → OPEN       after FAILURE_THRESHOLD consecutive failures
    OPEN       → HALF_OPEN  once RECOVERY_SECONDS have passed
    HALF_OPEN  → CLOSED     on one successful trial request
    HALF_OPEN  → OPEN       on a failed trial (cool-down restarts)

An OPEN tier is skipped without a request — the point is to stop hammering
a provider that's genuinely down, not to retry it on every student turn.

Mid-stream hot-swap: if a tier fails after it has already streamed part of
the answer, the next tier is told exactly what the student has already
received and asked to continue from that point. It's a prompt, not an
assistant prefill — Claude 5 models reject prefill — so continuity is
best-effort, but nothing already sent is ever retracted or repeated, and the
consumer's stream never ends early. Every chunk carries the tier position
that produced it.

Failure injection (FaultPlan) is how the Step 11 drill cuts the cable: it
aborts a *real* in-flight provider stream after N chunks — the HTTP response
is closed underneath the SDK — and raises a ProviderFailure, exactly what a
dropped connection looks like from here.

Status: standalone-verified (python -m app.fallback_cascade). Not yet spliced
into the swarm's own agent calls — see the README's wiring table.
"""

import logging
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger("fallback_cascade")

TIER_ORDER = ["primary", "secondary", "tertiary"]

TIER_MODELS = {
    "primary": os.getenv("CASCADE_PRIMARY_MODEL", "claude-sonnet-5"),
    "secondary": os.getenv("CASCADE_SECONDARY_MODEL", "claude-haiku-4-5-20251001"),
    "tertiary": "local-kb-responder",
}

FAILURE_THRESHOLD = int(os.getenv("CIRCUIT_FAILURE_THRESHOLD", "3"))
RECOVERY_SECONDS = float(os.getenv("CIRCUIT_RECOVERY_SECONDS", "30"))

SYSTEM_PROMPT = (
    "You are a campus IT helpdesk assistant. Answer the student's question "
    "concisely and practically, in plain prose with short numbered steps where useful."
)


class ProviderFailure(Exception):
    """A tier failed — before or during its stream."""


# ---- circuit breaker --------------------------------------------------------


class CircuitBreaker:
    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(
        self,
        position: str,
        failure_threshold: int = FAILURE_THRESHOLD,
        recovery_seconds: float = RECOVERY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.position = position
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._state = self.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self.times_opened = 0

    @property
    def state(self) -> str:
        with self._lock:
            return self._current_state()

    def _current_state(self) -> str:
        if self._state == self.OPEN and self._clock() - self._opened_at >= self.recovery_seconds:
            self._state = self.HALF_OPEN
        return self._state

    def allow_request(self) -> bool:
        with self._lock:
            return self._current_state() != self.OPEN

    def record_success(self) -> None:
        with self._lock:
            self._state = self.CLOSED
            self._consecutive_failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            state = self._current_state()
            self._consecutive_failures += 1
            if state == self.HALF_OPEN or self._consecutive_failures >= self.failure_threshold:
                if state != self.OPEN:
                    self.times_opened += 1
                    logger.warning("[cascade] circuit OPEN for %s after %d failure(s)", self.position, self._consecutive_failures)
                self._state = self.OPEN
                self._opened_at = self._clock()


# ---- tier providers ---------------------------------------------------------


@dataclass
class FaultPlan:
    """Cut a tier's stream after `after_chunks` chunks (0 = fail before the
    first chunk). Used by the Step 11 drill; empty in normal operation."""

    after_chunks: dict[str, int] = field(default_factory=dict)


def _model_stream(model: str) -> Callable[[list], Iterator[str]]:
    def stream(messages: list) -> Iterator[str]:
        from langchain_anthropic import ChatAnthropic  # local: the tertiary tier must work with no SDK/key

        llm = ChatAnthropic(model=model, max_retries=0)  # the cascade *is* the retry policy
        try:
            for chunk in llm.stream(messages):
                if chunk.text:
                    yield chunk.text
        except Exception as exc:  # noqa: BLE001 — any provider error is a tier failure
            raise ProviderFailure(f"{model}: {type(exc).__name__}: {exc}") from exc

    return stream


_WORD = re.compile(r"\S+\s*")


def _local_kb_stream(messages: list) -> Iterator[str]:
    """Tertiary tier: no model, no network. Picks the KB articles matching the
    question and streams them in word-sized chunks, so a consumer can't tell
    the difference in transport — only in the tier label."""
    from .tools.kb import search_kb

    question = next((m.content for m in messages if isinstance(m, HumanMessage)), "")
    question = question.split("STUDENT QUESTION:", 1)[-1]
    articles = search_kb.invoke({"query": question})
    if articles:
        body = " ".join(f"{a['id']} — {a['title']}: {a['body']}" for a in articles[:2])
        text = f"Our assistant is temporarily unavailable, so here is the matching help article: {body}"
    else:
        text = "Our assistant is temporarily unavailable. Please contact the IT service desk directly and we'll follow up."
    for word in _WORD.findall(text):
        yield word


def default_providers() -> dict[str, Callable[[list], Iterator[str]]]:
    return {
        "primary": _model_stream(TIER_MODELS["primary"]),
        "secondary": _model_stream(TIER_MODELS["secondary"]),
        "tertiary": _local_kb_stream,
    }


# ---- the cascade ------------------------------------------------------------


@dataclass
class Chunk:
    text: str
    tier: str  # position, never model name
    model: str


@dataclass
class TierSwitch:
    from_tier: str
    to_tier: str | None
    reason: str
    chars_already_sent: int


@dataclass
class TierSkipped:
    tier: str
    reason: str


def _messages_for(question: str, already_sent: str) -> list:
    if not already_sent:
        return [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=question)]
    return [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(
            content=(
                "Another assistant was answering this student and was cut off mid-answer. "
                "The student has ALREADY received the partial answer below. Continue it from "
                "exactly where it stops — do not repeat, restart or summarise anything already "
                "sent, and do not mention the interruption.\n\n"
                f"PARTIAL ANSWER ALREADY SENT:\n{already_sent}\n\n"
                f"STUDENT QUESTION:\n{question}"
            )
        ),
    ]


class FallbackCascade:
    def __init__(
        self,
        providers: dict[str, Callable[[list], Iterator[str]]] | None = None,
        models: dict[str, str] | None = None,
        breakers: dict[str, CircuitBreaker] | None = None,
    ):
        self.providers = providers or default_providers()
        self.models = models or dict(TIER_MODELS)
        self.breakers = breakers or {position: CircuitBreaker(position) for position in TIER_ORDER}
        self._lock = threading.Lock()
        self.health = {
            position: {"attempts": 0, "failures": 0, "successes": 0, "skipped_open": 0, "chars_served": 0}
            for position in TIER_ORDER
        }

    def _count(self, position: str, key: str, n: int = 1) -> None:
        with self._lock:
            self.health[position][key] += n

    def stream(self, question: str, faults: FaultPlan | None = None) -> Iterator[Chunk | TierSwitch | TierSkipped]:
        """Yields answer chunks interleaved with TierSkipped / TierSwitch
        events. The generator only ends once a tier finishes the answer, or
        after a final TierSwitch(to_tier=None) if every tier failed."""
        faults = faults or FaultPlan()
        sent = ""
        for index, position in enumerate(TIER_ORDER):
            breaker = self.breakers[position]
            if not breaker.allow_request():
                self._count(position, "skipped_open")
                yield TierSkipped(position, f"circuit {breaker.state}")
                continue

            self._count(position, "attempts")
            cut_after = faults.after_chunks.get(position)
            served = 0
            try:
                if cut_after == 0:
                    raise ProviderFailure(f"injected fault: {position} failed before its first chunk")
                stream = self.providers[position](_messages_for(question, sent))
                try:
                    for text in stream:
                        if cut_after is not None and served >= cut_after:
                            raise ProviderFailure(f"injected fault: {position} cut after {served} chunk(s)")
                        served += 1
                        sent += text
                        self._count(position, "chars_served", len(text))
                        yield Chunk(text=text, tier=position, model=self.models[position])
                finally:
                    # Closing the generator closes the provider's HTTP stream
                    # underneath it — on an injected fault that's the "cut".
                    close = getattr(stream, "close", None)
                    if close:
                        close()
            except ProviderFailure as exc:
                breaker.record_failure()
                self._count(position, "failures")
                next_position = TIER_ORDER[index + 1] if index + 1 < len(TIER_ORDER) else None
                yield TierSwitch(from_tier=position, to_tier=next_position, reason=str(exc), chars_already_sent=len(sent))
                continue

            breaker.record_success()
            self._count(position, "successes")
            return

    def health_report(self) -> dict:
        """Keyed by tier position. Step 11's monitoring tripwires read this."""
        with self._lock:
            return {
                position: {**self.health[position], "model": self.models[position], "circuit": self.breakers[position].state}
                for position in TIER_ORDER
            }


@dataclass
class Transcript:
    text: str
    segments: list[tuple[str, str, str]]  # (position, model, text) — consecutive chunks merged
    events: list
    served_by: list[str]


def collect(events: Iterator) -> Transcript:
    """Drain a cascade stream into a per-tier transcript."""
    segments: list[tuple[str, str, str]] = []
    other = []
    for event in events:
        if isinstance(event, Chunk):
            if segments and segments[-1][0] == event.tier:
                position, model, text = segments[-1]
                segments[-1] = (position, model, text + event.text)
            else:
                segments.append((event.tier, event.model, event.text))
        else:
            other.append(event)
    return Transcript(
        text="".join(text for *_, text in segments),
        segments=segments,
        events=other,
        served_by=[position for position, *_ in segments],
    )


# ---- standalone verification ------------------------------------------------


def _fake(chunks: list[str], fail_at: int | None = None) -> Callable[[list], Iterator[str]]:
    def stream(messages: list) -> Iterator[str]:
        for i, chunk in enumerate(chunks):
            if fail_at is not None and i == fail_at:
                raise ProviderFailure(f"simulated provider error at chunk {i}")
            yield chunk

    return stream


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _verify() -> list[tuple[str, bool, str]]:
    results = []

    # 1. breaker state machine
    clock = _FakeClock()
    breaker = CircuitBreaker("primary", failure_threshold=3, recovery_seconds=30, clock=clock)
    trail = [breaker.state]
    for _ in range(3):
        breaker.record_failure()
        trail.append(breaker.state)
    blocked = not breaker.allow_request()
    clock.now = 31
    trail.append(breaker.state)
    breaker.record_failure()
    trail.append(breaker.state)
    clock.now = 62
    breaker.record_success()
    trail.append(breaker.state)
    expected = ["closed", "closed", "closed", "open", "half_open", "open", "closed"]
    results.append(("circuit breaker: closed→open→half_open→open→closed", trail == expected and blocked, " → ".join(trail)))

    # 2. mid-stream hot-swap, attributed per tier
    cascade = FallbackCascade(
        providers={
            "primary": _fake(["Forget ", "and ", "rejoin ", "CampusSecure, ", "then "], fail_at=4),
            "secondary": _fake(["disable ", "Wi-Fi ", "power-saving."]),
            "tertiary": _fake(["(unused)"]),
        },
        models={"primary": "model-a", "secondary": "model-b", "tertiary": "local"},
    )
    t = collect(cascade.stream("wifi drops"))
    switch = next(e for e in t.events if isinstance(e, TierSwitch))
    ok = (
        t.served_by == ["primary", "secondary"]
        and t.segments[0][2] == "Forget and rejoin CampusSecure, "
        and t.segments[1][2] == "disable Wi-Fi power-saving."
        and switch.chars_already_sent == len("Forget and rejoin CampusSecure, ")
    )
    results.append(("mid-stream hot-swap: primary chunks then secondary chunks, one stream", ok, " | ".join(f"[{p}] {s!r}" for p, _, s in t.segments)))

    # 3. an open circuit is skipped without a request
    calls = []

    def counting(messages):
        calls.append(1)
        yield "x"

    cascade = FallbackCascade(
        providers={"primary": counting, "secondary": _fake(["served by secondary"]), "tertiary": _fake(["-"])},
        models={"primary": "a", "secondary": "b", "tertiary": "c"},
    )
    for _ in range(FAILURE_THRESHOLD):
        cascade.breakers["primary"].record_failure()
    t = collect(cascade.stream("q"))
    ok = calls == [] and t.served_by == ["secondary"] and isinstance(t.events[0], TierSkipped)
    results.append(("open circuit: primary skipped, zero requests sent to it", ok, f"primary requests={len(calls)} served_by={t.served_by}"))

    # 4. two positions sharing one model stay separate
    cascade = FallbackCascade(
        providers={"primary": _fake(["a", "b"], fail_at=1), "secondary": _fake(["c"]), "tertiary": _fake(["-"])},
        models={"primary": "same-model", "secondary": "same-model", "tertiary": "local"},
    )
    collect(cascade.stream("q"))
    h = cascade.health_report()
    ok = h["primary"]["failures"] == 1 and h["secondary"]["successes"] == 1 and h["secondary"]["failures"] == 0
    results.append(("shared model, separate tiers: counts stay per position", ok, f"primary={h['primary']['failures']} fail, secondary={h['secondary']['successes']} ok"))

    # 5. every model down → local KB tier still answers
    cascade = FallbackCascade(
        providers={"primary": _fake(["x"], fail_at=0), "secondary": _fake(["y"], fail_at=0), "tertiary": _local_kb_stream},
        models={"primary": "a", "secondary": "b", "tertiary": "local-kb-responder"},
    )
    t = collect(cascade.stream("My Wi-Fi keeps dropping in the library"))
    ok = t.served_by == ["tertiary"] and "KB-102" in t.text
    results.append(("both models down: tertiary local KB responder answers", ok, t.text[:90] + "…"))

    # 6. all three down → stream still ends cleanly with a terminal switch
    cascade = FallbackCascade(
        providers={p: _fake(["x"], fail_at=0) for p in TIER_ORDER},
        models={p: p for p in TIER_ORDER},
    )
    t = collect(cascade.stream("q"))
    last = t.events[-1]
    ok = t.text == "" and isinstance(last, TierSwitch) and last.to_tier is None
    results.append(("all tiers down: stream ends with a terminal event, not an exception", ok, f"{len(t.events)} switch events"))
    return results


def _live_demo() -> bool:
    """Real Anthropic streams: primary cut after 12 chunks, secondary continues."""
    question = "My laptop won't connect to the CampusSecure Wi-Fi in the library. What should I try?"
    cascade = FallbackCascade()
    start = time.monotonic()
    t = collect(cascade.stream(question, FaultPlan(after_chunks={"primary": 12})))
    elapsed = time.monotonic() - start
    print(f"\nLive: '{question}'  ({elapsed:.1f}s)")
    for event in t.events:
        print(f"  event: {event}")
    for position, model, text in t.segments:
        print(f"\n  ── served by {position} ({model}), {len(text)} chars ──\n  {text.strip()}")
    print(f"\n  health: {cascade.health_report()}")
    return t.served_by == ["primary", "secondary"]


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    print("Step 10 — fallback cascade standalone verification\n")
    checks = _verify()
    for name, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}\n        {detail}")
    live_ok = True
    if os.getenv("ANTHROPIC_API_KEY") and "--offline" not in sys.argv:
        live_ok = _live_demo()
        print(f"\n  {'PASS' if live_ok else 'FAIL'}  live: real primary stream cut mid-answer, real secondary continued it")
    passed = all(ok for _, ok, _ in checks) and live_ok
    print(f"\nStep 10 checklist: {'PASS' if passed else 'FAIL'} — cascade reports which tier served which part of the response")
    sys.exit(0 if passed else 1)
