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
| 2 | Gateway + sync endpoint | `POST /analyze` | ⏳ |
| 3 | Streaming (SSE) | `POST /analyze/stream` with typed events | ⏳ |
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
- **Idempotency in action:** on a two-issue ticket, both specialists sometimes draft the same `account` ticket. LangGraph also re-runs `hitl_gate` from the top on each resume. Both cases hit the same `thread_id + category` key, so the ticket store ends up with one ticket per run. A `created: false` in the write outcomes is the de-dupe working as designed.
