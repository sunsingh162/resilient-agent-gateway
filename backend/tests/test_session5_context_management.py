"""Session 5 unit tests: summarize_node's trimming logic runs without a live
LLM call by mocking the summarizer.
"""

from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

from app.graph.nodes import context as context_module


def _make_history(n: int) -> list:
    messages = []
    for i in range(n):
        messages.append(HumanMessage(content=f"Question {i}", id=f"human-{i}"))
        messages.append(AIMessage(content=f"Answer {i}", id=f"ai-{i}"))
    return messages


def test_below_threshold_is_a_noop_without_calling_the_llm():
    messages = _make_history(2)  # 4 messages, below default threshold of 10
    state = {"messages": messages, "summary": None}

    with patch.object(
        context_module,
        "get_summarizer_llm",
        side_effect=AssertionError("summarizer must not be called below the threshold"),
    ):
        result = context_module.summarize_node(state)

    assert result == {}


def test_above_threshold_trims_and_summarizes():
    messages = _make_history(6)  # 12 messages, above default threshold of 10
    state = {"messages": messages, "summary": None}

    class FakeSummary:
        text = "Fake condensed summary of the older turns."

    with patch.object(context_module, "get_summarizer_llm") as get_llm:
        get_llm.return_value.invoke.return_value = FakeSummary()
        result = context_module.summarize_node(state)

    assert result["summary"] == "Fake condensed summary of the older turns."

    removed_ids = {op.id for op in result["messages"] if isinstance(op, RemoveMessage)}
    kept_ids = {m.id for m in messages[-context_module.KEEP_RECENT_COUNT :]}
    expected_removed_ids = {m.id for m in messages[: -context_module.KEEP_RECENT_COUNT]}

    assert removed_ids == expected_removed_ids
    assert removed_ids.isdisjoint(kept_ids)


def test_existing_summary_is_folded_into_the_prompt():
    messages = _make_history(6)
    state = {"messages": messages, "summary": "Prior summary text."}

    class FakeSummary:
        text = "Updated combined summary."

    with patch.object(context_module, "get_summarizer_llm") as get_llm:
        get_llm.return_value.invoke.return_value = FakeSummary()
        context_module.summarize_node(state)

    prompt_messages = get_llm.return_value.invoke.call_args[0][0]
    human_prompt = prompt_messages[-1].content
    assert "Prior summary text." in human_prompt
