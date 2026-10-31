"""Phase 4, Step 9 checklist: swapping an agent's tier visibly changes which
model handles its calls — confirmed from the router's ledger AND from the
LLM spans Phoenix recorded independently.

Usage: python backend/scripts/run_step9_router.py
Requires ANTHROPIC_API_KEY and a running Phoenix (see README).

Three runs of the same ticket, each on a fresh thread:

    A. default tiers                 specialist=standard, classify=utility
    B. tiers swapped                 specialist=frontier, classify=economy
    C. frontier budget exhausted     specialist still *assigned* frontier,
                                     but routed down to standard

Runs call runner.run_ticket() directly, so the semantic cache (Step 8)
can't serve a repeat and every run really goes through the router.
"""

import sys
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import model_router, phoenix_client, tracing  # noqa: E402
from app.graph.runner import resolve_approval, run_ticket  # noqa: E402

TICKET = "The projector in lecture theatre 3 shows no signal from my laptop over HDMI. How do I fix it?"


def _run(label: str) -> tuple[str, dict[str, set[str]]]:
    thread_id = f"step9-{label}-{uuid.uuid4().hex[:8]}"
    start = time.monotonic()
    result = run_ticket(thread_id, TICKET)
    while result.get("__interrupt__"):  # a drafted ticket pauses at hitl_gate; approve so the turn finishes
        result = resolve_approval(thread_id, {"action": "approve"})
    elapsed = time.monotonic() - start

    rows = model_router.calls(thread_id=thread_id)
    by_agent: dict[str, set[str]] = {}
    print(f"\n=== Run {label}  thread={thread_id}  ({elapsed:.1f}s, status={'completed' if result.get('response') else '?'})")
    print(f"  {'agent':<12}{'assigned':<10}{'routed to':<11}{'model':<28}{'in':>7}{'out':>7}{'cost':>10}")
    for row in rows:
        by_agent.setdefault(row["agent"], set()).add(row["model"])
        print(
            f"  {row['agent']:<12}{row['requested_tier']:<10}{row['tier']:<11}{row['model']:<28}"
            f"{row['input_tokens']:>7}{row['output_tokens']:>7}{row['cost_usd']:>10.5f}"
        )
    print(f"  total ${sum(r['cost_usd'] for r in rows):.4f} across {len(rows)} LLM calls")
    return thread_id, by_agent


def _phoenix_models(thread_id: str) -> dict[str, set[str]]:
    """agent → models, read from Phoenix's LLM spans. OpenInference copies
    each call's metadata onto its span, and Phoenix's REST API returns it
    flattened: metadata.thread_id, metadata.router.agent, metadata.router.tier
    sit next to the llm.model_name the instrumentor records on its own."""
    tracing.flush_tracing()
    time.sleep(3)
    found: dict[str, set[str]] = {}
    for span in phoenix_client.get_spans(span_kind="LLM", limit=500):
        attrs = span.get("attributes") or {}
        if attrs.get("metadata.thread_id") == thread_id and attrs.get("metadata.router.agent"):
            found.setdefault(attrs["metadata.router.agent"], set()).add(attrs.get("llm.model_name"))
    return found


def main() -> None:
    if tracing.setup_tracing() is None:
        raise SystemExit("tracing is disabled (TRACING_ENABLED=false) — the Phoenix confirmation needs it")

    _, run_a = _run("A-defaults")

    model_router.set_agent_tier("specialist", "frontier")
    model_router.set_agent_tier("classify", "economy")
    thread_b, run_b = _run("B-swapped")
    phoenix_b = _phoenix_models(thread_b)
    print("\n  Phoenix LLM spans for run B (llm.model_name by router.agent):")
    for agent, models in sorted(phoenix_b.items()):
        print(f"    {agent:<12} {sorted(models)}")

    # Exhaust the frontier tier's virtual key: anything already spent is over a $0 budget.
    model_router.TIER_BUDGETS_USD["frontier"] = 0.0
    _, run_c = _run("C-frontier-over-budget")

    standard, frontier = model_router.TIER_MODELS["standard"], model_router.TIER_MODELS["frontier"]
    utility, economy = model_router.TIER_MODELS["utility"], model_router.TIER_MODELS["economy"]
    checks = [
        ("A: specialist on standard tier", run_a.get("specialist") == {standard}),
        ("A: classify on utility tier", run_a.get("classify") == {utility}),
        ("B: specialist moved to frontier", run_b.get("specialist") == {frontier}),
        ("B: classify moved to economy", run_b.get("classify") == {economy}),
        ("B: untouched agents unchanged", run_b.get("synthesizer") == run_a.get("synthesizer") == {standard}),
        ("B: Phoenix spans agree with the ledger", phoenix_b.get("specialist") == {frontier} and phoenix_b.get("classify") == {economy}),
        ("C: over-budget frontier downgraded to standard", run_c.get("specialist") == {standard}),
    ]
    print("\n=== Step 9 checks")
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    status = model_router.status()["tiers"]
    print("\n  tier spend (rolling window):")
    for tier, info in status.items():
        flag = "OVER BUDGET" if info["over_budget"] else ""
        print(f"    {tier:<9} {info['model']:<28} spent ${info['spent_usd']:.4f} of ${info['budget_usd']:.2f} {flag}")

    passed = all(ok for _, ok in checks)
    print(f"\nStep 9 checklist: {'PASS' if passed else 'FAIL'} — tier swaps change the serving model, confirmed in Phoenix")
    tracing.shutdown_tracing()
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
