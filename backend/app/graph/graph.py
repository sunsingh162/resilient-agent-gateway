"""Session 11: human-in-the-loop approval.

hitl_gate_node sits between the parallel specialist dispatch and the
synthesizer. It's a node native to THIS graph — not invoked via a nested
subgraph .invoke() the way specialist dispatch is — which matters: interrupt()
only pauses the specific compiled graph it's called within, and this is the
graph the FastAPI layer actually calls run_ticket()/get_state() on. The
create_ticket tool (used inside the nested specialist subgraph) no longer
writes anything itself — it only drafts a proposal — so hitl_gate_node's
call to ticket_store.create_ticket_record is the ONLY place in this project
that write ever actually happens, and it's unreachable without first going
through interrupt() and an explicit Command(resume=...) decision.
"""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from .nodes.dispatcher import (
    determine_categories_node,
    dispatch_to_specialists,
    parallel_specialist_worker_node,
    synthesizer_node,
)
from .nodes.guardrails import egress_guardrail_node
from .nodes.hitl import hitl_gate_node
from .state import TicketState
from .subgraphs.triage import build_triage_subgraph


def route_after_triage(state: TicketState) -> str:
    return "egress" if state.get("injection_blocked") else "determine_categories"


def build_graph(checkpointer: BaseCheckpointSaver | None = None):
    graph = StateGraph(TicketState)

    graph.add_node("triage", build_triage_subgraph())
    graph.add_node("determine_categories", determine_categories_node)
    graph.add_node("parallel_specialist_worker", parallel_specialist_worker_node)
    graph.add_node("hitl_gate", hitl_gate_node)
    graph.add_node("synthesizer", synthesizer_node)
    graph.add_node("egress", egress_guardrail_node)

    graph.add_edge(START, "triage")
    graph.add_conditional_edges(
        "triage",
        route_after_triage,
        {"egress": "egress", "determine_categories": "determine_categories"},
    )
    graph.add_conditional_edges("determine_categories", dispatch_to_specialists, ["parallel_specialist_worker"])
    graph.add_edge("parallel_specialist_worker", "hitl_gate")
    graph.add_edge("hitl_gate", "synthesizer")
    graph.add_edge("synthesizer", "egress")
    graph.add_edge("egress", END)

    return graph.compile(checkpointer=checkpointer)
