"""Phase 4, Step 3: typed Server-Sent Event schemas for /analyze/stream.

Every event on the wire is

    event: <type>
    data: {"type": "<type>", "seq": n, "elapsed_ms": t, ...payload}

so a client can switch on either the SSE `event:` line or the JSON `type`
field. `seq` is strictly increasing per stream; `elapsed_ms` is measured
from the moment the gateway accepted the request, which is what makes
"events arrive incrementally, not all at once" checkable from the client.
"""

import json
from typing import Literal, Optional, Union

from pydantic import BaseModel


class _Event(BaseModel):
    seq: int = 0
    elapsed_ms: int = 0


class RunStarted(_Event):
    type: Literal["run_started"] = "run_started"
    thread_id: str


class AgentHandoff(_Event):
    """A master-graph node finished and control moves on. `detail` carries
    the one decision that node made (category, dispatch list, ...)."""

    type: Literal["agent_handoff"] = "agent_handoff"
    node: str
    detail: dict = {}


class ToolCall(_Event):
    type: Literal["tool_call"] = "tool_call"
    specialist: Optional[str]
    tool: str
    args: dict


class ToolResult(_Event):
    type: Literal["tool_result"] = "tool_result"
    specialist: Optional[str]
    tool: str
    content: str


class SpecialistFinding(_Event):
    type: Literal["specialist_finding"] = "specialist_finding"
    specialist: str
    finding: str


class ApprovalRequired(_Event):
    type: Literal["approval_required"] = "approval_required"
    category: str
    draft: dict


class FinalAnswer(_Event):
    type: Literal["final_answer"] = "final_answer"
    response: Optional[str]


class StreamError(_Event):
    type: Literal["error"] = "error"
    message: str


class Done(_Event):
    type: Literal["done"] = "done"
    status: Literal["completed", "awaiting_approval", "blocked", "error"]


StreamEvent = Union[
    RunStarted, AgentHandoff, ToolCall, ToolResult, SpecialistFinding, ApprovalRequired, FinalAnswer, StreamError, Done
]


def to_sse(event: StreamEvent) -> str:
    return f"event: {event.type}\ndata: {json.dumps(event.model_dump(), default=str)}\n\n"
