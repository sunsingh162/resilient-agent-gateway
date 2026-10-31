"""Phase 4, Step 5: OpenTelemetry tracing exported to Arize Phoenix.

Two layers of spans, one trace per request:

1. A root `helpdesk.turn` span opened by the gateway (turn_span below) —
   carries what only the gateway knows: endpoint, thread_id, final status,
   latency, dispatch categories. Steps 6 (judge) and 7 (monitoring) query
   Phoenix for exactly these attributes.
2. Everything underneath comes from OpenInference's LangChainInstrumentor,
   which hooks LangChain's callback system: one span per LangGraph node,
   per LLM call (with token counts) and per tool call. Because it rides on
   callbacks, it also reaches inside the nested specialist .invoke() — the
   same propagation path Step 3's streaming relies on — so parallel
   specialists show up as separate subtrees, not one flat span.

Tracing is best-effort by design: if Phoenix isn't running the exporter
logs a connection warning and drops spans; no request ever fails because
of tracing. TRACING_ENABLED=false skips setup entirely.
"""

import logging
import os
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

logger = logging.getLogger("tracing")

PHOENIX_PROJECT_NAME = os.getenv("PHOENIX_PROJECT_NAME", "resilient-agent-gateway")
PHOENIX_COLLECTOR_ENDPOINT = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006")

_tracer_provider = None


def tracing_enabled() -> bool:
    return os.getenv("TRACING_ENABLED", "true").lower() not in ("0", "false", "no")


def setup_tracing():
    """Idempotent. Registers a global tracer provider exporting to Phoenix
    and instruments LangChain/LangGraph. Imports are local so a process
    with tracing disabled never pays Phoenix's import cost."""
    global _tracer_provider
    if _tracer_provider is not None or not tracing_enabled():
        return _tracer_provider

    from openinference.instrumentation.langchain import LangChainInstrumentor
    from phoenix.otel import register

    # The Anthropic SDK now emits its own `anthropic.messages.create` span
    # whenever a global tracer provider exists. OpenInference already records
    # every one of those calls as an LLM span (with token counts), so leaving
    # the SDK's on would double every LLM call — and double-count tokens and
    # cost in the monitoring (Step 7) and cost (Steps 8–9) layers. Must be set
    # before the first ChatAnthropic client is built; ours are built lazily.
    os.environ.setdefault("ANTHROPIC_OPEN_TELEMETRY", "false")

    _tracer_provider = register(
        project_name=PHOENIX_PROJECT_NAME,
        endpoint=f"{PHOENIX_COLLECTOR_ENDPOINT}/v1/traces",
        batch=True,
        set_global_tracer_provider=True,
        verbose=False,
    )
    LangChainInstrumentor().instrument(tracer_provider=_tracer_provider)
    logger.info("[tracing] exporting to %s (project=%s)", PHOENIX_COLLECTOR_ENDPOINT, PHOENIX_PROJECT_NAME)
    return _tracer_provider


def shutdown_tracing() -> None:
    """Flush buffered spans — BatchSpanProcessor would otherwise drop the
    last few seconds of spans when the process exits."""
    if _tracer_provider is not None:
        _tracer_provider.force_flush()
        _tracer_provider.shutdown()


def get_tracer():
    return trace.get_tracer("resilient-agent-gateway")


@contextmanager
def turn_span(endpoint: str, thread_id: str, message: str):
    """Root span for one helpdesk turn. Yields the span so the caller can
    record the outcome via record_turn_result() once it knows it."""
    attributes = {
        "openinference.span.kind": "CHAIN",
        "input.value": message,
        "session.id": thread_id,
        "helpdesk.thread_id": thread_id,
        "helpdesk.endpoint": endpoint,
    }
    # start_as_current_span already records the exception event and sets
    # ERROR status (with "Type: message") when one escapes — only the
    # helpdesk-specific attribute needs adding here.
    with get_tracer().start_as_current_span("helpdesk.turn", attributes=attributes) as span:
        try:
            yield span
        except Exception:
            span.set_attribute("helpdesk.status", "error")
            raise


def record_turn_result(span, status: str, latency_ms: int, result: dict) -> None:
    span.set_attribute("helpdesk.status", status)
    span.set_attribute("helpdesk.latency_ms", latency_ms)
    span.set_attribute("helpdesk.escalated", bool(result.get("escalated")))
    if result.get("category"):
        span.set_attribute("helpdesk.category", result["category"])
    if result.get("dispatch_categories"):
        span.set_attribute("helpdesk.dispatch_categories", list(result["dispatch_categories"]))
    if result.get("response"):
        span.set_attribute("output.value", result["response"])
    if status == "error":
        span.set_status(Status(StatusCode.ERROR))
    else:
        span.set_status(Status(StatusCode.OK))
