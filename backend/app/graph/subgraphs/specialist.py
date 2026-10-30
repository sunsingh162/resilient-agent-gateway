"""Specialist subgraph: the ReAct loop that answers an already-triaged ticket.

Takes state with category/messages/summary already populated by the triage
subgraph and runs agent <-> tools until it has a final response.
Independently compiled and invocable on its own.
"""

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from ..nodes.agent import TOOLS, agent_node, finalize_node, route_after_agent
from ..state import TicketState


def build_specialist_subgraph():
    graph = StateGraph(TicketState)

    graph.add_node("agent", agent_node)
    graph.add_node("tools", ToolNode(TOOLS))
    graph.add_node("finalize", finalize_node)

    graph.add_edge(START, "agent")
    graph.add_conditional_edges(
        "agent",
        route_after_agent,
        {"tools": "tools", "finalize": "finalize"},
    )
    graph.add_edge("tools", "agent")
    graph.add_edge("finalize", END)

    return graph.compile()
