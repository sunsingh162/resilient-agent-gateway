"""Phase 4, Step 3: translate a live LangGraph run into typed SSE events.

Two LangGraph stream modes, combined:

- "updates" (master graph + the triage subgraph via subgraphs=True): one
  chunk per node as it finishes → agent_handoff / specialist_finding /
  approval_required, plus the turn's final response.
- "messages": LLM chunks and tool messages from *inside* the nested
  specialist ReAct loops. Those loops run via a plain .invoke() inside
  parallel_specialist_worker_node, so they never show up as "updates" on the
  outer stream — but LangChain's callback propagation still carries their
  messages out, which is the only way to surface a tool call while the
  specialist is still working instead of 8s later when it returns.

Tool calls stream in as partial AIMessageChunks; they're accumulated per
message id and emitted once the final chunk arrives, so a client gets one
complete tool_call event with parsed args, not a trickle of JSON fragments.
"""

import contextvars
import queue
import threading
import time
from collections.abc import Iterator

from langchain_core.messages import AIMessageChunk, ToolMessage

from .graph import approvals
from .graph.runner import _build_turn_input, _config, get_compiled_graph
from .stream_schemas import (
    AgentHandoff,
    ApprovalRequired,
    Done,
    FinalAnswer,
    RunStarted,
    SpecialistFinding,
    StreamError,
    StreamEvent,
    ToolCall,
    ToolResult,
)
from .tracing import record_turn_result, turn_span

# The one decision each node made that's worth showing on a handoff event.
_HANDOFF_DETAIL_KEYS = ("category", "dispatch_categories", "injection_blocked", "escalated", "write_outcomes")
_TOOL_RESULT_PREVIEW_CHARS = 400


def _handoff_detail(update: dict) -> dict:
    return {key: update[key] for key in _HANDOFF_DETAIL_KEYS if key in update and update[key] not in (None, [], {})}


def stream_analysis(thread_id: str, message: str) -> Iterator[StreamEvent]:
    """Step 5: runs the turn on one dedicated producer thread, inside the
    turn's root span, and hands events to the HTTP layer through a queue.

    Why a thread: Starlette iterates a sync generator by calling next() on
    whichever threadpool worker is free, each with a fresh copy of the
    context. A span opened inside the generator would be attached in one
    context and detached in another ("Failed to detach context"), and the
    instrumentor's child spans would lose their parent. One thread owning
    the whole turn keeps the span's context in one place. Side effect, and
    the right one for a helpdesk: a client that disconnects mid-stream
    doesn't abort the turn — it still finishes and gets checkpointed.
    """
    events: queue.Queue = queue.Queue()

    def produce() -> None:
        summary: dict = {}
        status = "error"
        start = time.monotonic()
        try:
            with turn_span("/analyze/stream", thread_id, message) as span:
                for event in _stream_events(thread_id, message):
                    if isinstance(event, AgentHandoff):
                        summary.update({k: v for k, v in event.detail.items() if k in ("category", "dispatch_categories", "escalated")})
                    elif isinstance(event, FinalAnswer):
                        summary["response"] = event.response
                    elif isinstance(event, Done):
                        status = event.status
                    events.put(event)
                record_turn_result(span, status, int((time.monotonic() - start) * 1000), summary)
        finally:
            events.put(_END_OF_STREAM)

    # Run the producer inside a copy of the caller's context, so the turn's
    # root span is parented to the POST /analyze/stream HTTP span the same
    # way /analyze's is, instead of starting a separate trace.
    request_context = contextvars.copy_context()
    threading.Thread(target=request_context.run, args=(produce,), name=f"stream-{thread_id}", daemon=True).start()
    while (event := events.get()) is not _END_OF_STREAM:
        yield event


_END_OF_STREAM = object()


def _stream_events(thread_id: str, message: str) -> Iterator[StreamEvent]:
    start = time.monotonic()
    seq = 0

    def stamp(event: StreamEvent) -> StreamEvent:
        nonlocal seq
        seq += 1
        event.seq = seq
        event.elapsed_ms = int((time.monotonic() - start) * 1000)
        return event

    yield stamp(RunStarted(thread_id=thread_id))

    graph = get_compiled_graph()
    config = _config(thread_id)
    turn_input = _build_turn_input(graph, thread_id, message)

    tool_call_chunks: dict[str, AIMessageChunk] = {}
    interrupts: list = []
    final_response: str | None = None
    injection_blocked = False

    try:
        for namespace, mode, chunk in graph.stream(
            turn_input, config, stream_mode=["updates", "messages"], subgraphs=True
        ):
            if mode == "messages":
                msg, meta = chunk
                node = meta.get("langgraph_node")
                specialist = meta.get("specialist")

                if node == "agent" and isinstance(msg, AIMessageChunk):
                    acc = tool_call_chunks.get(msg.id)
                    acc = msg if acc is None else acc + msg
                    tool_call_chunks[msg.id] = acc
                    if msg.chunk_position == "last":
                        del tool_call_chunks[msg.id]
                        for call in acc.tool_calls:
                            yield stamp(ToolCall(specialist=specialist, tool=call["name"], args=call["args"]))

                elif node == "tools" and isinstance(msg, ToolMessage):
                    yield stamp(
                        ToolResult(
                            specialist=specialist,
                            tool=msg.name or "unknown",
                            content=str(msg.content)[:_TOOL_RESULT_PREVIEW_CHARS],
                        )
                    )
                continue

            # mode == "updates"
            prefix = "/".join(part.split(":")[0] for part in namespace)
            for node, update in chunk.items():
                if node == "__interrupt__":
                    for item in update:
                        interrupts.append(item)
                        value = item.value if isinstance(item.value, dict) else {}
                        yield stamp(ApprovalRequired(category=value.get("category", "unknown"), draft=value.get("draft", {})))
                    continue

                update = update if isinstance(update, dict) else {}
                if update.get("response") is not None:
                    final_response = update["response"]
                if update.get("injection_blocked"):
                    injection_blocked = True

                if node == "parallel_specialist_worker":
                    for specialist, finding in (update.get("specialist_findings") or {}).items():
                        yield stamp(SpecialistFinding(specialist=specialist, finding=finding or ""))
                else:
                    name = f"{prefix}/{node}" if prefix else node
                    yield stamp(AgentHandoff(node=name, detail=_handoff_detail(update)))

    except Exception as exc:  # noqa: BLE001 — the stream must end with a typed event, never a dropped socket
        yield stamp(StreamError(message=f"{type(exc).__name__}: {exc}"))
        yield stamp(Done(status="error"))
        return

    # Same registry sync runner.run_ticket() does after invoke(), so
    # /pending-approvals sees writes paused from a streamed turn too.
    approvals.sync_from_result(thread_id, {"__interrupt__": interrupts})

    if interrupts:
        status = "awaiting_approval"
    else:
        status = "blocked" if injection_blocked else "completed"
        yield stamp(FinalAnswer(response=final_response))
    yield stamp(Done(status=status))
