"""Triage subgraph: ingress guardrails -> classify -> summarize.

Independently compiled and invocable on its own (see
tests/test_session7_subgraphs.py) — the master graph composes this as a
single node, but nothing about it depends on the master graph existing.
"""

from langgraph.graph import END, START, StateGraph

from ..nodes.classify import classify_node
from ..nodes.context import summarize_node
from ..nodes.guardrails import (
    blocked_response_node,
    ingress_guardrail_node,
    route_after_ingress,
)
from ..state import TicketState


def build_triage_subgraph():
    graph = StateGraph(TicketState)

    graph.add_node("ingress", ingress_guardrail_node)
    graph.add_node("blocked_response", blocked_response_node)
    graph.add_node("classify", classify_node)
    graph.add_node("summarize", summarize_node)

    graph.add_edge(START, "ingress")
    graph.add_conditional_edges(
        "ingress",
        route_after_ingress,
        {"blocked_response": "blocked_response", "classify": "classify"},
    )
    graph.add_edge("blocked_response", END)
    graph.add_edge("classify", "summarize")
    graph.add_edge("summarize", END)

    return graph.compile()
