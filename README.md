# Resilient Agent Gateway

**Domain chosen (Problem Statement, Option 1): Productionize your Phase 3 project.** The base swarm is the Campus IT Helpdesk Agent from Phase 3 ([`sunsingh162/campus-it-helpdesk-agent`](https://github.com/sunsingh162/campus-it-helpdesk-agent)). It triages password, network, hardware and account tickets, fans out to parallel specialists, and puts any ticket write behind human approval. Phase 4 adds the production stack on top, one step per commit.

**Submission format: Option B, repo only.** Phoenix tracing runs as a local process, and multi-agent turns are too long for serverless timeouts.

> 🚧 **Work in progress.** This README grows with each step. The step table below shows what's done.

---

## Quickstart

```bash
git clone https://github.com/sunsingh162/resilient-agent-gateway.git
cd resilient-agent-gateway

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and set ANTHROPIC_API_KEY=sk-ant-... (no quotes)

cd backend
python -m pytest tests/ -q        # deterministic tests pass with no key; live ones auto-skip without it
python scripts/run_swarm.py       # Step 1: one full swarm run from a plain script
```

**Run the full stack** in two terminals from the repo root, with the venv active in each:

```bash
# Terminal 1: Phoenix trace UI and collector on :6006 (first start takes ~15s)
PHOENIX_WORKING_DIR=backend/data/phoenix phoenix serve

# Terminal 2: the gateway on :8000
cd backend && uvicorn app.main:app --port 8000
```

Phoenix is optional. If it isn't running, the gateway works normally and tracing just drops its spans.

---

## Phase 4 build: one step per commit

| # | Step | Adds | Status |
|---|---|---|---|
| 1 | Confirm the base swarm | Phase 3 swarm imported unchanged + `scripts/run_swarm.py` end-to-end runner | ✅ |
| 2 | Gateway + sync endpoint | `POST /analyze` (blocks until the swarm finishes or pauses at HITL) + `phase4_live_step` on `/health` | ✅ |
| 3 | Streaming (SSE) | `POST /analyze/stream`: 9 typed events (`stream_schemas.py`), including tool calls from inside the parallel specialists while they run | ✅ |
| 4 | Async job queue | `POST /analyze/async` → 202 + `GET /jobs/{id}`. Jobs are stored in SQLite, so results survive client disconnects and server restarts | ✅ |
| 5 | Tracing | OpenTelemetry → Arize Phoenix: a root `helpdesk.turn` span per request, plus one span per graph node, LLM call and tool call, including inside the parallel specialists | ✅ |
| 6 | LLM-as-judge | Grade traces on 2+ axes | ⏳ |
| 7 | Monitoring daemon | Named SLOs + paging tripwires | ⏳ |
| 8 | Semantic cache | Embedding cache + cost model | ⏳ |
| 9 | Model router | Per-agent cost tiers + budget | ⏳ |
| 10 | Fallback cascade | Tier-keyed cascade + circuit breaker + mid-stream hot-swap | ⏳ |
| 11 | Failure drill | Self-contained "cut cable" script + cascade tripwires | ⏳ |
| 12 | Wiring audit | Live-wired vs standalone honesty table | ⏳ |

---

## Gateway API

Start it from `backend/`:

```bash
uvicorn app.main:app --port 8000
```

Interactive docs are at `http://localhost:8000/docs`.

| Endpoint | Step | Purpose |
|---|---|---|
| `POST /analyze` | 2 | Sync. Sends a ticket and blocks until the swarm finishes the turn. `thread_id` is optional: omit it to start a new conversation. |
| `POST /analyze/stream` | 3 | Same turn as `/analyze`, streamed as Server-Sent Events |
| `POST /analyze/async` | 4 | Returns `202 Accepted` with a `job_id` immediately and runs the turn on a worker pool |
| `GET /jobs/{job_id}` | 4 | Job status (`queued → running → completed / awaiting_approval / blocked / failed`). Once finished, `result` has the same shape as the `/analyze` response. Unknown IDs return `404` |
| `GET /health` | 2 | Liveness check, plus `phase4_live_step`: the highest step that is live-wired into request handling |
| `POST /approvals/{thread_id}/approve` · `/deny` · `/edit-approve` | Phase 3 | Resolve a write paused at the HITL gate |
| `GET /pending-approvals` · `GET /forensics/{thread_id}` · `POST /tickets/run` · `POST /tickets/stream` | Phase 3 | Unchanged from Phase 3 |

**Step 2 checklist (a curl returns a complete result after blocking):**

```bash
curl -s -X POST localhost:8000/analyze -H 'Content-Type: application/json' \
  -d '{"message":"My Wi-Fi keeps dropping every few minutes in the library. Any fix?"}'
```

The call blocks for about 15s and returns `"status": "completed"` with the full synthesised `response`, `dispatch_categories` and `latency_ms`. A ticket that needs staff action, such as `"My student account S1002 is locked"`, returns `"status": "awaiting_approval"` with each draft in `pending_writes`. Call `/approvals/{thread_id}/approve` once per draft to finish the turn.

### Streaming events (Step 3)

Every event is `event: <type>` plus `data: {"type", "seq", "elapsed_ms", ...}`. `elapsed_ms` counts from when the gateway accepted the request.

| Event | When | Payload |
|---|---|---|
| `run_started` | Request accepted | `thread_id` |
| `agent_handoff` | A graph node finishes | `node` (e.g. `triage/classify`, `determine_categories`), `detail` (the decision it made) |
| `tool_call` | A specialist decides to call a tool | `specialist`, `tool`, `args` (complete and parsed) |
| `tool_result` | The tool returns | `specialist`, `tool`, `content` (first 400 chars) |
| `specialist_finding` | One parallel specialist finishes | `specialist`, `finding` |
| `approval_required` | Paused at the HITL gate | `category`, `draft` |
| `final_answer` | Turn complete (not sent while awaiting approval) | `response` |
| `error` | Anything raised mid-run | `message` (the stream still closes cleanly with `done`) |
| `done` | Always last | `status`: `completed` / `awaiting_approval` / `blocked` / `error` |

**Step 3 checklist (events arrive incrementally under `curl -N`):**

```bash
curl -N -X POST localhost:8000/analyze/stream -H 'Content-Type: application/json' \
  -d '{"message":"My Wi-Fi keeps dropping in the library, and can you check if my student account S1002 is locked?"}'
```

Recorded run, with arrival time measured on the client:

```
 0.0s  #1  run_started
 1.7s  #3  agent_handoff       triage/classify → network
 3.1s  #6  agent_handoff       determine_categories → [network, account]
 5.0s  #7  tool_call           account · lookup_student_account {"student_id": "S1002"}
 5.2s  #11 tool_call           network · lookup_student_account {"student_id": "S1002"}
 7.0s  #15 tool_call           account · assess_severity
 9.0s  #21 tool_call           account · create_ticket
13.2s  #25 specialist_finding  account
13.6s  #26 specialist_finding  network
13.6s  #27 approval_required
13.6s  #28 done                awaiting_approval
```

A low-severity ticket ends with `final_answer` and `done: completed` at about 13s. A prompt-injection attempt ends with `done: blocked` in 8ms, with no LLM call.

### Async jobs (Step 4)

**Step 4 checklist (submit a job, close the client, poll later and still get the result):**

```bash
# 1. Submit. curl exits as soon as the 202 arrives, so the client is gone.
curl -s -X POST localhost:8000/analyze/async -H 'Content-Type: application/json' \
  -d '{"message":"My Wi-Fi keeps dropping every few minutes in the library. Any fix?"}'
# → HTTP 202 in 4ms: {"job_id":"job-20c5…","status":"queued","poll_url":"/jobs/job-20c5…"}

# 2. Poll from a new client whenever you like.
curl -s localhost:8000/jobs/<job_id>
# → "running" at first; ~13s later: "completed" with the full result
```

Recorded run: 202 returned in **4ms**. An immediate poll showed `running`. The next poll showed `completed`, with `started_at` at 05:24:18 and `finished_at` at 05:24:31, and the full synthesised response in `result`.

**Past the checklist, also verified live:**
- **Finished jobs survive a server restart.** I stopped uvicorn, started it again, and polled the same `job_id`: still `completed`, with the full result.
- **A job killed mid-run doesn't hang.** I submitted a job, stopped the server while it was `running`, and restarted it. The startup hook logs `marked 1 job(s) from a previous process as failed`, and polling returns `failed` with the error `server restarted before this job finished — resubmit it`.

### Tracing (Step 5)

Open **http://localhost:6006** and choose project **`resilient-agent-gateway`**. Each `/analyze`, `/analyze/stream` or `/analyze/async` call produces one trace:

```
POST /analyze                                  ← FastAPI's native HTTP span
└─ helpdesk.turn                               ← gateway root span (app/tracing.py)
   └─ LangGraph                                ← OpenInference LangChainInstrumentor from here down
      ├─ triage → ingress, classify (ChatAnthropic), summarize
      ├─ determine_categories (ChatAnthropic)
      ├─ parallel_specialist_worker [network]
      │  └─ LangGraph → agent (ChatAnthropic) → tools → search_kb, assess_severity …
      ├─ parallel_specialist_worker [account]
      │  └─ LangGraph → agent → tools → lookup_student_account, create_ticket …
      ├─ hitl_gate
      └─ synthesizer (ChatAnthropic) → egress
```

`helpdesk.turn` carries the attributes that Steps 6 and 7 query: `helpdesk.endpoint`, `helpdesk.thread_id` (also `session.id`), `helpdesk.status`, `helpdesk.latency_ms`, `helpdesk.category`, `helpdesk.dispatch_categories`, `helpdesk.escalated`, `input.value` and `output.value`. A turn that raises ends with status `ERROR` and an `exception` event.

**Step 5 checklist (a real run shows nested spans, not one flat span).** Recorded from real runs, read back from Phoenix's REST API:

| Endpoint | Spans in trace | LLM spans | Tool spans | Path to a tool span |
|---|---|---|---|---|
| `/analyze` (2 specialists, paused for approval) | 64 | 10 | 8 | `create_ticket ← tools ← LangGraph ← parallel_specialist_worker ← LangGraph ← helpdesk.turn ← POST /analyze` |
| `/analyze/stream` (1 specialist, completed) | 36 | 5 | 2 | `assess_severity ← tools ← LangGraph ← parallel_specialist_worker ← LangGraph ← helpdesk.turn ← POST /analyze/stream` |
| `/analyze/async` (failed: out of API credit) | 10 | 1 | 0 | Trace status `ERROR`, with Anthropic's 400 message on the span |

**Three tracing problems found and fixed during this step:**
- **Every LLM call was traced twice.** The Anthropic SDK now emits its own `anthropic.messages.create` span whenever a global tracer provider exists, on top of OpenInference's `ChatAnthropic` span. That would double-count tokens and cost in Steps 7 to 9. Fixed with `ANTHROPIC_OPEN_TELEMETRY=false`, which `tracing.setup_tracing()` sets. Now there are 0 duplicate spans.
- **The stream raised `Failed to detach context`.** Starlette runs each `next()` of a sync generator on a different threadpool worker, so a span opened inside the generator got closed in another thread's context. Fixed by running the turn on one producer thread that holds the span and hands events over through a queue. The thread starts in a copy of the request's context, so the stream's trace hangs under its `POST /analyze/stream` span. Now there are 0 errors in the gateway log.
- **Trace noise.** FastAPI's native telemetry added `fastapi.dependencies` and `fastapi.serialization` spans to every request, plus a one-span trace for every `GET /jobs` poll and `/health` check. Per-operation spans are off, and those two paths are excluded. I verified 3 polls plus 3 health checks added 0 traces.

---

## Base swarm (from Phase 3)

```
START → triage (ingress guardrail → classify → summarize)
      → determine_categories (LLM picks 1–3)
      → parallel_specialist_worker ×N (Send fan-out; each is an agent ⇄ tools ReAct loop)
      → hitl_gate (interrupt() before any ticket write)
      → synthesizer → egress guardrail → END
```

Full Phase 3 architecture, session history and honesty notes: [`docs/phase3-base-swarm.md`](docs/phase3-base-swarm.md).

---

## Honesty notes (kept current per step)

- **Step 1:** the Phase 3 code was imported with one fix. The Anthropic API now rejects an explicit `temperature` on `claude-haiku-4-5` (`400: temperature: Extra inputs are not permitted`), so `classify.py` drops it, the same way `agent.py` already did for `claude-sonnet-5`. `run_swarm.py` auto-approves every HITL-paused write so a smoke run can finish, and it prints how many it approved.
- **Step 3 touched one Phase 3 file.** `dispatcher.py` now passes `metadata={"specialist": category}` into the nested specialist `.invoke()`. Without it, the specialists' tool calls stream out with no link to the parallel branch that made them, so two concurrent specialists can't be told apart. It's metadata only, with no behaviour change, and all 50 Phase 3 tests still pass.
- **How tool calls get out of the nested graphs:** the specialist ReAct loops run through a plain `.invoke()` inside a node, so LangGraph's `updates` mode only shows them when the whole specialist returns, about 8s later. The live `tool_call` and `tool_result` events come from `messages` mode instead, which LangChain's callback propagation carries out of the nested run.
- **Approvals stream one at a time.** `hitl_gate` interrupts once per drafted write, so a stream ends at the *first* `approval_required`. The remaining drafts surface on the next resume through `/approvals/{thread_id}/approve`, exactly as in Phase 3.
- **Step 4's queue is in-process, not a broker.** Jobs run on a `ThreadPoolExecutor` inside the API process. The job *record* is durable (SQLite), but a job that is mid-run when the process dies is lost and gets marked `failed`. It isn't retried. A broker-backed queue (Celery, RQ, Arq) would hand it to another worker instead. That was out of scope here because the swarm, its checkpointer and its approval registry all already live in this one process.
- **An async job that pauses for approval stays `awaiting_approval`.** Resolving it through `/approvals/{thread_id}/*` finishes the conversation but doesn't update the job record. The job's `result.thread_id` is the handle for continuing it.
- **Step 5: async jobs start their own trace.** `/analyze/async` returns its 202 and closes its HTTP span before the job runs, so the job's `helpdesk.turn` is a root span on the worker thread. It's tagged `helpdesk.endpoint=/analyze/async`. Sync and stream turns are children of their HTTP span.
- **Step 5: the approval endpoints have no `helpdesk.turn` span.** `/approvals/{thread_id}/*` resumes are still traced by OpenInference, because their graph runs appear in Phoenix, but they don't get the gateway's root span or `helpdesk.*` attributes.
- **Idempotency in action:** on a two-issue ticket, both specialists sometimes draft the same `account` ticket. LangGraph also re-runs `hitl_gate` from the top on each resume. Both cases hit the same `thread_id + category` key, so the ticket store ends up with one ticket per run. A `created: false` in the write outcomes is the de-dupe working as designed.
