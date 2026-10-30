import json
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

load_dotenv()

from .graph import approvals  # noqa: E402
from .graph.forensics import state_forensics  # noqa: E402
from .graph.runner import get_compiled_graph, resolve_approval, run_ticket, stream_ticket  # noqa: E402

app = FastAPI(title="Campus IT Helpdesk Agent")

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


def _to_ticket_response(thread_id: str, result: dict) -> TicketResponse:
    return TicketResponse(
        thread_id=thread_id,
        category=result.get("category"),
        response=result.get("response"),
        escalated=result.get("escalated", False),
        pending_approval=bool(result.get("__interrupt__")),
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


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
