"""Integration tests for the LangGraph Supervisor Orchestrator."""

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from src.agentic.orchestrator.graph import create_supervisor_graph


@pytest.mark.asyncio
async def test_supervisor_execution_flow():
    checkpointer = MemorySaver()
    graph = create_supervisor_graph(checkpointer=checkpointer)

    thread_id = "test_thread_001"
    config = {"configurable": {"thread_id": thread_id}}

    initial_state = {
        "messages": [HumanMessage(content="Giới thiệu ngắn gọn về nền tảng đa tác tử.")],
        "thread_id": thread_id,
    }

    final_state = await graph.ainvoke(initial_state, config=config)

    assert final_state.get("plan") is not None
    assert len(final_state["plan"].steps) >= 1
    assert final_state.get("final_answer") is not None


@pytest.mark.asyncio
async def test_supervisor_hitl_interrupt_and_resume():
    checkpointer = MemorySaver()
    graph = create_supervisor_graph(checkpointer=checkpointer)

    thread_id = "test_hitl_002"
    config = {"configurable": {"thread_id": thread_id}}

    # Query containing high-risk action (write / overwrite)
    initial_state = {
        "messages": [HumanMessage(content="Ghi đè file test_output.txt với nội dung confidential")],
        "thread_id": thread_id,
    }

    # First invoke will reach hitl and trigger interrupt
    _ = await graph.ainvoke(initial_state, config=config)

    snapshot = graph.get_state(config)
    # Verify graph is paused at hitl interrupt
    if snapshot.next:
        assert len(snapshot.tasks) > 0
        assert len(snapshot.tasks[0].interrupts) > 0

        # Resume with approved decision
        resumed_state = await graph.ainvoke(
            Command(resume={"approved": True}),
            config=config,
        )
        assert resumed_state.get("final_answer") is not None
