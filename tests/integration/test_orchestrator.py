"""Integration tests for the LangGraph Supervisor Orchestrator."""

from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from src.agentic.orchestrator.graph import create_supervisor_graph
from src.agentic.orchestrator.nodes.reflector import ReflectionEvaluation


class _FakeStructuredJudge:
    """Deterministic reflection judge; the real one needs an external LLM."""

    async def ainvoke(self, prompt: str) -> ReflectionEvaluation:
        return ReflectionEvaluation(
            passed=True,
            confidence_score=0.95,
            critique="Deterministic test judge: goal addressed.",
            action="proceed",
        )


class _FakeLLM:
    def with_structured_output(self, schema: Any) -> _FakeStructuredJudge:
        return _FakeStructuredJudge()

    async def ainvoke(self, prompt: Any) -> AIMessage:
        return AIMessage(content="Deterministic synthesis grounded in [step_1].")


class _FakeA2AClient:
    """Deterministic sub-agent stream; no A2A/LiteLLM servers in integration tests."""

    async def call_agent_stream(self, **kwargs: Any):  # type: ignore[no-untyped-def]
        yield {"event": "token", "token": "done"}
        yield {"event": "completed", "output": "Deterministic step output"}


@pytest.mark.asyncio
async def test_supervisor_execution_flow(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAST_HEURISTIC_PLANNER", "true")
    checkpointer = MemorySaver()
    graph = create_supervisor_graph(checkpointer=checkpointer)

    thread_id = "test_thread_001"
    config = {"configurable": {"thread_id": thread_id}}

    initial_state = {
        "messages": [HumanMessage(content="Giới thiệu ngắn gọn về nền tảng đa tác tử.")],
        "thread_id": thread_id,
    }

    with (
        patch(
            "src.agentic.orchestrator.nodes.dispatch.get_a2a_client",
            return_value=_FakeA2AClient(),
        ),
        patch(
            "src.agentic.orchestrator.nodes.reflector.get_llm",
            return_value=_FakeLLM(),
        ),
        patch(
            "src.agentic.orchestrator.nodes.finalize.get_llm",
            return_value=_FakeLLM(),
        ),
    ):
        final_state = await graph.ainvoke(initial_state, config=config)

    assert final_state.get("plan") is not None
    assert len(final_state["plan"].steps) >= 1
    assert final_state.get("final_answer") is not None


@pytest.mark.asyncio
async def test_supervisor_hitl_interrupt_and_resume(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAST_HEURISTIC_PLANNER", "true")
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

        # Resume with approved decision. External LLM/A2A services are stubbed
        # so the HITL resume path itself is what gets exercised.
        with (
            patch(
                "src.agentic.orchestrator.nodes.dispatch.get_a2a_client",
                return_value=_FakeA2AClient(),
            ),
            patch(
                "src.agentic.orchestrator.nodes.reflector.get_llm",
                return_value=_FakeLLM(),
            ),
            patch(
                "src.agentic.orchestrator.nodes.finalize.get_llm",
                return_value=_FakeLLM(),
            ),
        ):
            resumed_state = await graph.ainvoke(
                Command(resume={"approved": True}),
                config=config,
            )
        assert resumed_state.get("final_answer") is not None
    else:
        pytest.fail(
            f"Expected the supervisor graph to pause at the HITL node, got next={snapshot.next!r}"
        )
