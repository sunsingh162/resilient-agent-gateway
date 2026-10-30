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

---

## Phase 4 build: one step per commit

| # | Step | Adds | Status |
|---|---|---|---|
| 1 | Confirm the base swarm | Phase 3 swarm imported unchanged + `scripts/run_swarm.py` end-to-end runner | ✅ |
| 2 | Gateway + sync endpoint | `POST /analyze` (blocks until the swarm finishes or pauses at HITL) + `phase4_live_step` on `/health` | ✅ |
| 3 | Streaming (SSE) | `POST /analyze/stream`: 9 typed events (`stream_schemas.py`), including tool calls from inside the parallel specialists while they run | ✅ |
| 4 | Async job queue | `POST /analyze/async` → 202 + `GET /jobs/{id}` | ⏳ |
| 5 | Tracing | OpenTelemetry spans → Arize Phoenix | ⏳ |
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
- **Idempotency in action:** on a two-issue ticket, both specialists sometimes draft the same `account` ticket. LangGraph also re-runs `hitl_gate` from the top on each resume. Both cases hit the same `thread_id + category` key, so the ticket store ends up with one ticket per run. A `created: false` in the write outcomes is the de-dupe working as designed.
