# Campus IT Helpdesk Agent

**Domain chosen (Problem Statement, Option 1):** Campus IT Helpdesk Agent — triages password/network/hardware/account tickets, looks up student accounts and KB articles, and files a follow-up ticket for staff when an issue is severe enough — gated behind human approval before it ever actually writes anything.

A multi-session LangGraph agent system built incrementally, one production capability per session, following the Phase 3 Step-by-Step Guide's progression. Submission format: **Option B — repo only** (no hosted deployment; long-running multi-agent turns don't suit serverless timeouts, and this needs local SQLite files).

---

## Quickstart

```bash
git clone <this-repo>
cd "Agentic AI Phase 3 Project"

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and set ANTHROPIC_API_KEY=sk-ant-... (get one at console.anthropic.com)

cd backend
python -m pytest tests/ -v       # 50 tests, ~35s, needs the API key for most of them
uvicorn app.main:app --reload --port 8000
```

Then open `frontend/index.html` directly in a browser (no build step) — it talks to `http://localhost:8000` by default (editable in the UI).

Each session's own checklist can be re-run individually:
```bash
cd backend
python scripts/run_session2_demo.py   # ... through run_session12_demo.py
```
(Sessions 1, 4, 7 don't have a separate demo script — their checklist is covered by the pytest suite itself; see the session table below.)

---

## Architecture

One `StateGraph` (`backend/app/graph/graph.py`), evolved incrementally across 12 sessions. Current shape:

```
START
  ↓
┌───────────────────────────────────────────────────────────────┐
│ triage  (subgraph — independently compiled & tested on its own) │
│                                                                   │
│   ingress (PII redaction + prompt-injection check)               │
│     ├─ injection detected → blocked_response → (subgraph ends)   │
│     └─ clean → classify → summarize → (subgraph ends)            │
└───────────────────────────────────────────────────────────────┘
  ↓
  ├─ injection_blocked ─────────────────────────────────────→ egress → END
  └─ clean ↓
     determine_categories (LLM picks 1–3 relevant categories)
     ↓
     dispatch_to_specialists ──Send──→ parallel_specialist_worker (×N,
                                        concurrent — each runs the
                                        specialist subgraph: agent ⇄
                                        tools loop with its own
                                        iteration cap)
     ↓ (waits for all N to finish)
     hitl_gate (any drafted ticket write pauses here via interrupt(),
                until an explicit approve/deny/edit-and-approve)
     ↓
     synthesizer (reads all findings + write outcomes, produces ONE
                  response, filtered to this turn's categories)
     ↓
     egress (PII/hedging scan on the outgoing response)
     ↓
    END
```

Two **independently compiled subgraphs** are composed into the master graph (each is separately invocable and tested on its own — see `tests/test_session7_subgraphs.py`):
- **Triage subgraph** (`subgraphs/triage.py`): ingress → classify → summarize
- **Specialist subgraph** (`subgraphs/specialist.py`): agent ⇄ tools → finalize — this is the actual ReAct loop, reused as the engine behind every parallel dispatch

**Persistence:** every turn goes through `SqliteSaver`, keyed by `thread_id`. A conversation survives a full process restart — proven live in the Session 4 checklist (kill the `uvicorn` process, restart it, continue the same thread).

**Bound tools** (4, all available to the specialist's ReAct loop):
| Tool | Type | What it does |
|---|---|---|
| `lookup_student_account` | mock CRM/data-lookup | Looks up a student's enrollment status, devices, lock status |
| `search_kb` | mock knowledge-base search | Returns matching troubleshooting articles |
| `assess_severity` | classifier | Rule-based low/medium/high/critical severity assessment |
| `create_ticket` | **gated write** | Drafts a proposed ticket — never writes directly (see Session 10/11 below) |

---

## Session table

| # | Session | What it added |
|---|---|---|
| 1 | Graph skeleton | Typed `TicketState`, `classify_node`, category-specific stub handlers |
| 2 | Tool binding | `lookup_student_account` + `search_kb` tools, `agent_node` with `bind_tools()` + `ToolNode`, tool-call loop |
| 3 | ReAct loop | Hard `MAX_ITERATIONS` cap, duplicate-tool-call fingerprint detection (circuit breaker), `assess_severity` tool |
| 4 | Persistence & threading | `SqliteSaver` checkpointer, `run_ticket()`/`stream_ticket()` entry points, FastAPI `/tickets/run` — conversations survive a server restart |
| 5 | Context management | `summarize_node` folds older turns into a running summary + trims message history once past a threshold |
| 6 | Guardrails | Ingress: PII redaction + regex prompt-injection detection (routes straight to a blocked response, zero LLM tokens spent). Egress: PII-leak + hedging-phrase scan before delivery |
| 7 | Multi-agent topologies | Flat graph split into two independently compiled, independently testable subgraphs (triage, specialist) |
| 8 | Supervisor orchestrator | Sequential hub-and-spoke supervisor with `MAX_DELEGATIONS` cap — **superseded by Session 9's parallel dispatch in the live master graph, but its code and tests are untouched and still pass** (see Honesty note below) |
| 9 | Parallel specialists | `Send`-based fan-out to 2–3 specialists concurrently in one superstep, a `merge_findings` reducer (required once concurrent writes are possible), and a real LLM-synthesized unified response |
| 10 | Gated write access | `create_ticket` tool, `ticket_store.py` (mock ticket system, SQLite), idempotency key = `hash(thread_id + category)`, hard severity gate |
| 11 | Human-in-the-loop approval | The write moved out of the tool entirely — `hitl_gate_node` is the *only* place `create_ticket_record` is ever called, and it's unreachable without `interrupt()` + an explicit `Command(resume=...)`. Approve/deny/edit-and-approve endpoints, minimal approval-panel UI |
| 12 | Time travel & forensics | `state_forensics()` (3 anomaly detectors), `find_bad_checkpoint()`, `apply_correction()`, `time_travel()` — proven live: corrupt a checkpoint, detect it, fix it, and watch a real node (`egress`) re-execute from the corrected point while the original corrupted branch stays in history |

Git history has one commit per session, in order — see `git log --oneline`.

---

## Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.11 |
| Agent framework | LangGraph 1.2.x + langchain-core |
| LLM | Anthropic Claude — `claude-sonnet-5` (agent/supervisor/dispatcher/synthesizer), `claude-haiku-4-5` (classify/summarize, cheaper for simple tasks) |
| Persistence | SQLite via `langgraph-checkpoint-sqlite` (conversation checkpoints); separate SQLite tables for the mock ticket store and the pending-approvals registry |
| Backend API | FastAPI + Uvicorn |
| Frontend | Plain HTML/CSS/JS, no framework or build step |
| Testing | pytest (50 tests; deterministic ones need no API key, live ones auto-skip without `ANTHROPIC_API_KEY`) |

---

## API endpoints

| Endpoint | Purpose |
|---|---|
| `POST /tickets/run` | Send a message on a `thread_id`, get the full response back |
| `POST /tickets/stream` | Same, but streams state as newline-delimited JSON |
| `GET /pending-approvals` | List every write currently paused for human approval, across all threads |
| `POST /approvals/{thread_id}/approve` | Approve the next pending write on that thread |
| `POST /approvals/{thread_id}/deny` | Deny it |
| `POST /approvals/{thread_id}/edit-approve` | Edit fields (e.g. description) and approve |
| `GET /forensics/{thread_id}` | Full checkpoint timeline for a thread, with anomaly flags |
| `GET /health` | Liveness check |

Interactive docs at `http://localhost:8000/docs` once the server is running.

---

## Honesty — what's real vs. mocked

- **Mock data, by design (explicitly in scope per the Problem Statement):** `lookup_student_account` has 3 hardcoded students; `search_kb` has 4 canned articles; `create_ticket` writes to a local SQLite table, not a real ticketing system.
- **Session 8 vs. Session 9:** the sequential supervisor (`nodes/supervisor.py`) is fully built and still passes its own tests (`test_session8_supervisor.py`), but the *live* master graph (`graph.py`) uses Session 9's parallel dispatcher instead — the supervisor code is kept as evidence of the incremental build, the same way Session 7's subgraphs stayed in use even after later sessions built on top of them. It is not dead code by accident; it's a deliberate historical snapshot.
- **Idempotency key scope (Session 10):** `create_ticket`'s idempotency key is `hash(thread_id + category)`, not raw description text — chosen deliberately so a multi-issue conversation can still file one ticket per genuinely different category, at the cost of not deduplicating on exact wording. Documented and tested in `test_session10_gated_write.py`.
- **LLM non-determinism:** `claude-sonnet-5` doesn't support `temperature=0`, so whether the agent decides to propose a ticket for a borderline-severity ticket isn't 100% repeatable run to run. Deterministic tests (Sessions 3, 8, 9, 11, 12) seed state directly rather than depending on a live model decision; the live demo scripts still exercise the real model end-to-end, and note where a result may vary.
- **A real bug we found and fixed:** every SQLite path defaulted to a *relative* string, which resolved differently depending on which directory the process was launched from — running the server via `cd backend && uvicorn ...` silently wrote to `backend/backend/data/` instead of `backend/data/`. Fixed in `app/paths.py` by anchoring relative paths to the repo root instead of the process's cwd. Two earlier commits briefly had the wrong nested files tracked in git; they've been removed from tracking going forward (see `.gitignore`).
