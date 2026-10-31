import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

load_dotenv()

from .graph import approvals  # noqa: E402
from .graph.forensics import state_forensics  # noqa: E402
from .graph.runner import get_compiled_graph, resolve_approval, run_ticket, stream_ticket  # noqa: E402
from . import cost_model, job_queue, job_store, model_router, semantic_cache  # noqa: E402
from .stream_schemas import to_sse  # noqa: E402
from .streaming import stream_analysis  # noqa: E402
from .tracing import record_turn_result, setup_tracing, shutdown_tracing, turn_span  # noqa: E402

logger = logging.getLogger("gateway")


def _is_untraced_path(path: str) -> bool:
    return path == "/health" or path.startswith("/jobs/")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_tracing()
    orphaned = job_store.fail_orphaned_jobs()
    if orphaned:
        logger.warning("[gateway] marked %d job(s) from a previous process as failed", orphaned)
    yield
    shutdown_tracing()


app = FastAPI(
    title="Resilient Agent Gateway — Campus IT Helpdesk Swarm",
    lifespan=lifespan,
    # FastAPI's native OTel: keep the per-request HTTP span (it parents the
    # helpdesk.turn span), drop the per-operation dependencies/serialization
    # spans that add a few dozen rows of noise to every trace, and skip
    # health checks and job polls entirely — a client polling /jobs every 2s
    # would otherwise bury the real agent traces in one-span GET traces.
    telemetry={"operation_spans": False, "exclude": lambda scope: _is_untraced_path(scope.get("path", ""))},
)

# Dev-only convenience: the minimal frontend (frontend/index.html) is a
# static file with no build step, typically opened straight from disk or
# served on a different port — allow it to call this API locally.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class TicketRequest(BaseModel):
    thread_id: str
    message: str


class TicketResponse(BaseModel):
    thread_id: str
    category: Optional[str]
    response: Optional[str]
    escalated: bool
    pending_approval: bool


class EditAndApproveRequest(BaseModel):
    edited_fields: Optional[dict] = None


class TimelineEntryResponse(BaseModel):
    checkpoint_id: str
    next: list[str]
    category: Optional[str]
    response: Optional[str]
    iteration_count: int
    escalated: bool
    anomalies: list[str]


class AnalyzeRequest(BaseModel):
    message: str
    # Optional: omit it to start a fresh conversation, pass a prior one to
    # continue it (same checkpointer-backed continuity as /tickets/run).
    thread_id: Optional[str] = None


class PendingWrite(BaseModel):
    category: str
    draft: dict


class CacheInfo(BaseModel):
    status: Literal["hit", "miss", "bypass"]
    reason: str
    similarity: Optional[float] = None


class AnalyzeResponse(BaseModel):
    thread_id: str
    # completed: the swarm finished and `response` is final.
    # awaiting_approval: paused at hitl_gate — resolve via /approvals/{thread_id}/*.
    # blocked: ingress guardrail stopped it (prompt injection) before any LLM call.
    status: Literal["completed", "awaiting_approval", "blocked"]
    category: Optional[str]
    dispatch_categories: list[str]
    response: Optional[str]
    pending_writes: list[PendingWrite]
    escalated: bool
    latency_ms: int
    # Step 8: the semantic cache's decision for this turn, and what the turn
    # actually cost in API spend (0 on a cache hit).
    cache: Optional[CacheInfo] = None
    cost_usd: Optional[float] = None


def _pending_writes(result: dict) -> list[PendingWrite]:
    return [
        PendingWrite(category=item.value["category"], draft=item.value["draft"])
        for item in result.get("__interrupt__") or []
        if isinstance(item.value, dict) and item.value.get("type") == "write_approval"
    ]


def _to_analyze_response(thread_id: str, result: dict, latency_ms: int) -> AnalyzeResponse:
    if result.get("__interrupt__"):
        status = "awaiting_approval"
    elif result.get("injection_blocked"):
        status = "blocked"
    else:
        status = "completed"
    return AnalyzeResponse(
        thread_id=thread_id,
        status=status,
        category=result.get("category"),
        dispatch_categories=result.get("dispatch_categories") or [],
        response=result.get("response"),
        pending_writes=_pending_writes(result),
        escalated=result.get("escalated", False),
        latency_ms=latency_ms,
    )


class JobAccepted(BaseModel):
    job_id: str
    thread_id: str
    status: str
    poll_url: str


class JobStatus(BaseModel):
    job_id: str
    thread_id: str
    status: Literal["queued", "running", "completed", "awaiting_approval", "blocked", "failed"]
    created_at: str
    started_at: Optional[str]
    finished_at: Optional[str]
    # Same shape as /analyze's response once the job reaches a terminal
    # status other than "failed"; null until then.
    result: Optional[AnalyzeResponse]
    error: Optional[str]


_CACHE_VOLATILE_FIELDS = {"thread_id", "latency_ms", "cache", "cost_usd"}


def _is_new_thread(thread_id: str) -> bool:
    snapshot = get_compiled_graph().get_state({"configurable": {"thread_id": thread_id}})
    return not snapshot.values.get("messages")


def _analyze_turn(thread_id: str, message: str, endpoint: str = "/analyze") -> AnalyzeResponse:
    # The root span is opened here, not in the endpoint, so an async job's
    # trace is rooted on the worker thread that actually runs the swarm.
    # Step 8: the semantic cache sits here too, so /analyze and
    # /analyze/async share it (scope rules in semantic_cache.py).
    with turn_span(endpoint, thread_id, message) as span:
        start = time.monotonic()
        decision = semantic_cache.lookup(message, is_new_thread=_is_new_thread(thread_id))
        cache_info = CacheInfo(status=decision.status, reason=decision.reason, similarity=decision.similarity)
        span.set_attribute("helpdesk.cache", decision.status)

        if decision.status == "hit":
            latency_ms = int((time.monotonic() - start) * 1000)
            response = AnalyzeResponse(
                **decision.cached_response,
                thread_id=thread_id,
                latency_ms=latency_ms,
                cache=cache_info,
                cost_usd=0.0,
            )
            span.set_attribute("helpdesk.cache.similarity", decision.similarity)
            record_turn_result(span, response.status, latency_ms, response.model_dump())
            return response

        with cost_model.measure_turn() as usage:
            result = run_ticket(thread_id, message)
        latency_ms = int((time.monotonic() - start) * 1000)
        response = _to_analyze_response(thread_id, result, latency_ms)
        response.cache = cache_info
        response.cost_usd = round(usage.cost_usd, 6)
        semantic_cache.finish(
            decision, message, response.model_dump(exclude=_CACHE_VOLATILE_FIELDS), usage.cost_usd, latency_ms
        )
        span.set_attribute("helpdesk.cost_usd", usage.cost_usd)
        record_turn_result(span, response.status, latency_ms, response.model_dump())
        return response


def _to_ticket_response(thread_id: str, result: dict) -> TicketResponse:
    return TicketResponse(
        thread_id=thread_id,
        category=result.get("category"),
        response=result.get("response"),
        escalated=result.get("escalated", False),
        pending_approval=bool(result.get("__interrupt__")),
    )


# The highest Phase 4 step that is actually live-wired into request handling,
# bumped only when a step's capability is on the real request path, not
# merely built and verified standalone.
PHASE4_LIVE_STEP = 9


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "phase4_live_step": PHASE4_LIVE_STEP}


@app.post("/analyze", response_model=AnalyzeResponse)
def analyze(payload: AnalyzeRequest) -> AnalyzeResponse:
    """Phase 4 Step 2: synchronous gateway endpoint — blocks until the swarm
    finishes the turn (or pauses at hitl_gate) and returns the full result.

    A plain `def` (not `async def`) on purpose: run_ticket() is blocking,
    so FastAPI runs it in its threadpool instead of stalling the event loop
    for every other request while a 15–20s multi-agent turn runs.
    """
    thread_id = payload.thread_id or f"analyze-{uuid.uuid4()}"
    return _analyze_turn(thread_id, payload.message)


@app.post("/analyze/async", response_model=JobAccepted, status_code=202)
def analyze_async(payload: AnalyzeRequest) -> JobAccepted:
    """Phase 4 Step 4: accept the turn, return a job id immediately (202),
    run it on the worker pool. The client can disconnect straight away —
    the result lands in job_store and is recovered via GET /jobs/{job_id}.
    """
    thread_id = payload.thread_id or f"job-thread-{uuid.uuid4()}"
    message = payload.message
    job, _ = job_queue.submit(
        thread_id, message, lambda: _analyze_turn(thread_id, message, endpoint="/analyze/async").model_dump()
    )
    return JobAccepted(
        job_id=job["job_id"],
        thread_id=thread_id,
        status=job["status"],
        poll_url=f"/jobs/{job['job_id']}",
    )


class TierAssignment(BaseModel):
    tier: Literal["economy", "utility", "standard", "frontier"]


@app.get("/router/status")
def router_status() -> dict:
    """Phase 4 Step 9: per-agent tier → model, and each tier's spend vs budget."""
    return model_router.status()


@app.put("/router/agents/{agent}")
def set_router_tier(agent: str, payload: TierAssignment) -> dict:
    """Reassign one agent's cost tier at runtime — applies to its next LLM call."""
    try:
        model_router.set_agent_tier(agent, payload.tier)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return model_router.status()["assignments"][agent]


@app.get("/cache/stats")
def cache_stats() -> dict:
    """Phase 4 Step 8: semantic cache hit rate and the cost model's dollars saved."""
    return semantic_cache.stats()


@app.get("/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str) -> JobStatus:
    job = job_store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id: {job_id}")
    return JobStatus(**{key: job[key] for key in JobStatus.model_fields})


@app.post("/analyze/stream")
def analyze_stream(payload: AnalyzeRequest) -> StreamingResponse:
    """Phase 4 Step 3: same turn as /analyze, streamed as Server-Sent Events —
    one typed event per handoff, tool call, tool result, specialist finding,
    approval pause and the final answer (schemas in stream_schemas.py).

    The generator is sync, so Starlette iterates it in a worker thread and
    flushes each event as soon as it's yielded.
    """
    thread_id = payload.thread_id or f"stream-{uuid.uuid4()}"
    events = (to_sse(event) for event in stream_analysis(thread_id, payload.message))
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        # no-transform/X-Accel-Buffering: stop proxies (nginx, etc.) from
        # buffering the stream into one blob, which would defeat the point.
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@app.post("/tickets/run", response_model=TicketResponse)
def run(payload: TicketRequest) -> TicketResponse:
    result = run_ticket(payload.thread_id, payload.message)
    return _to_ticket_response(payload.thread_id, result)


@app.post("/tickets/stream")
def stream(payload: TicketRequest) -> StreamingResponse:
    def event_generator():
        for state in stream_ticket(payload.thread_id, payload.message):
            chunk = {
                "category": state.get("category"),
                "response": state.get("response"),
                "pending_approval": bool(state.get("__interrupt__")),
            }
            yield json.dumps(chunk) + "\n"

    return StreamingResponse(event_generator(), media_type="application/x-ndjson")


@app.get("/pending-approvals")
def get_pending_approvals() -> list[dict]:
    return approvals.list_pending()


@app.post("/approvals/{thread_id}/approve", response_model=TicketResponse)
def approve(thread_id: str) -> TicketResponse:
    result = resolve_approval(thread_id, {"action": "approve"})
    return _to_ticket_response(thread_id, result)


@app.post("/approvals/{thread_id}/deny", response_model=TicketResponse)
def deny(thread_id: str) -> TicketResponse:
    result = resolve_approval(thread_id, {"action": "deny"})
    return _to_ticket_response(thread_id, result)


@app.post("/approvals/{thread_id}/edit-approve", response_model=TicketResponse)
def edit_approve(thread_id: str, payload: EditAndApproveRequest) -> TicketResponse:
    decision = {"action": "edit_and_approve", "edited_fields": payload.edited_fields or {}}
    result = resolve_approval(thread_id, decision)
    return _to_ticket_response(thread_id, result)


@app.get("/forensics/{thread_id}", response_model=list[TimelineEntryResponse])
def forensics(thread_id: str) -> list[TimelineEntryResponse]:
    graph = get_compiled_graph()
    timeline = state_forensics(graph, thread_id)
    return [
        TimelineEntryResponse(
            checkpoint_id=entry.checkpoint_id,
            next=list(entry.next),
            category=entry.category,
            response=entry.response,
            iteration_count=entry.iteration_count,
            escalated=entry.escalated,
            anomalies=entry.anomalies,
        )
        for entry in timeline
    ]
