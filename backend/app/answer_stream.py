"""Phase 4, Step 11: the fallback cascade (Step 10) behind a live SSE endpoint.

POST /answer/stream streams a helpdesk answer token by token through one
process-wide FallbackCascade — shared, so circuit-breaker state carries
across requests exactly as it would in production: three cut requests in a
row open the primary circuit, and the fourth request skips it.

Chaos (fault injection) is opt-in twice over: the gateway must be started
with CHAOS_ENABLED=true, and the request must carry
X-Chaos-Cut-Primary-After: N. A request asking for chaos on a gateway
without the flag is refused (403) rather than silently ignored — so a
drill can't "pass" against a gateway that never injected anything.

Every streamed answer is traced as a `cascade.answer` span. Its attributes
are what the Step 11 monitoring tripwires read:

    cascade.served_by            tiers that produced text, in order
    cascade.fallback             True if any tier but primary produced text
    cascade.switches             mid-request tier failures
    cascade.skipped_open         tiers skipped because their circuit was open
    cascade.exhausted            every tier failed
    cascade.circuit.<position>   breaker state after the request
    cascade.chaos                fault injected by a drill

The producer runs on its own thread for the same reason as /analyze/stream
(Step 5): one thread owns the span's context for the whole answer.
"""

import contextvars
import os
import queue
import threading
import time
from collections.abc import Iterator

from opentelemetry.trace import Status, StatusCode

from .fallback_cascade import TIER_ORDER, Chunk, FallbackCascade, FaultPlan, TierSkipped, TierSwitch
from .stream_schemas import AnswerDelta, AnswerDone, RunStarted, StreamError, StreamEvent, TierSkippedEvent, TierSwitched
from .tracing import get_tracer

_cascade: FallbackCascade | None = None
_cascade_lock = threading.Lock()


def chaos_enabled() -> bool:
    return os.getenv("CHAOS_ENABLED", "false").lower() in ("1", "true", "yes")


def get_cascade() -> FallbackCascade:
    global _cascade
    with _cascade_lock:
        if _cascade is None:
            _cascade = FallbackCascade()
        return _cascade


def reset_cascade() -> None:
    """Fresh breakers and health counters (tests, and between drill runs)."""
    global _cascade
    with _cascade_lock:
        _cascade = None


def _answer_events(request_id: str, question: str, faults: FaultPlan, chaos: bool) -> Iterator[StreamEvent]:
    cascade = get_cascade()
    start = time.monotonic()
    seq = 0

    def stamp(event):
        nonlocal seq
        seq += 1
        event.seq, event.elapsed_ms = seq, int((time.monotonic() - start) * 1000)
        return event

    served_by: list[str] = []
    chars_by_tier = {position: 0 for position in TIER_ORDER}
    switches = skipped = 0
    exhausted = False
    text = ""

    attributes = {
        "openinference.span.kind": "CHAIN",
        "input.value": question,
        "cascade.request_id": request_id,
        "cascade.chaos": chaos,
        "cascade.primary_circuit_at_start": cascade.breakers["primary"].state,
    }
    with get_tracer().start_as_current_span("cascade.answer", attributes=attributes) as span:
        yield stamp(RunStarted(thread_id=request_id))
        for event in cascade.stream(question, faults):
            if isinstance(event, Chunk):
                if not served_by or served_by[-1] != event.tier:
                    served_by.append(event.tier)
                chars_by_tier[event.tier] += len(event.text)
                text += event.text
                yield stamp(AnswerDelta(text=event.text, tier=event.tier, model=event.model))
            elif isinstance(event, TierSwitch):
                switches += 1
                exhausted = event.to_tier is None
                span.add_event("cascade.tier_switch", {"from": event.from_tier, "to": event.to_tier or "", "reason": event.reason})
                yield stamp(
                    TierSwitched(
                        from_tier=event.from_tier,
                        to_tier=event.to_tier,
                        reason=event.reason,
                        chars_already_sent=event.chars_already_sent,
                    )
                )
            elif isinstance(event, TierSkipped):
                skipped += 1
                yield stamp(TierSkippedEvent(tier=event.tier, reason=event.reason))

        circuits = {position: cascade.breakers[position].state for position in TIER_ORDER}
        span.set_attribute("cascade.served_by", served_by)
        span.set_attribute("cascade.fallback", any(tier != "primary" for tier in served_by))
        span.set_attribute("cascade.switches", switches)
        span.set_attribute("cascade.skipped_open", skipped)
        span.set_attribute("cascade.exhausted", exhausted)
        for position, state in circuits.items():
            span.set_attribute(f"cascade.circuit.{position}", state)
        span.set_attribute("output.value", text)
        span.set_status(Status(StatusCode.ERROR if exhausted else StatusCode.OK))
        yield stamp(
            AnswerDone(
                status="exhausted" if exhausted else "completed",
                served_by=served_by,
                chars_by_tier=chars_by_tier,
                circuits=circuits,
            )
        )


_END = object()


def stream_answer(request_id: str, question: str, faults: FaultPlan, chaos: bool) -> Iterator[StreamEvent]:
    events: queue.Queue = queue.Queue()

    def produce() -> None:
        try:
            for event in _answer_events(request_id, question, faults, chaos):
                events.put(event)
        except Exception as exc:  # noqa: BLE001 — anything the cascade doesn't absorb still ends as a typed event
            events.put(StreamError(message=f"{type(exc).__name__}: {exc}"))
        finally:
            events.put(_END)

    threading.Thread(target=contextvars.copy_context().run, args=(produce,), name=f"answer-{request_id}", daemon=True).start()
    while (event := events.get()) is not _END:
        yield event
