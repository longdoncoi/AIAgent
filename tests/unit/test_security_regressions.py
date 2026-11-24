"""Regression tests for cross-boundary authorization and state integrity."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from src.agentic.memory.redis_saver import RedisSaver
from src.agentic.memory.thread_registry import ThreadOwnershipError, ThreadRegistry
from src.agentic.orchestrator.state import Plan, Step
from src.agentic.tools.execution_context import (
    ToolExecutionContext,
    reset_tool_execution_context,
    set_tool_execution_context,
)
from src.agentic.tools.mcp_client import MCPClientManager


def test_plan_rejects_cycles_and_unknown_dependencies() -> None:
    with pytest.raises(ValidationError, match="cycle"):
        Plan(
            goal="invalid",
            steps=[
                Step(id="a", description="a", agent="chat", input_query="a", depends_on=["b"]),
                Step(id="b", description="b", agent="chat", input_query="b", depends_on=["a"]),
            ],
        )
    with pytest.raises(ValidationError, match="unknown dependencies"):
        Plan(
            goal="invalid",
            steps=[
                Step(
                    id="a",
                    description="a",
                    agent="chat",
                    input_query="a",
                    depends_on=["missing"],
                )
            ],
        )


def test_redis_saver_rejects_unsigned_pickle() -> None:
    import pickle

    saver = RedisSaver.__new__(RedisSaver)
    saver.settings = SimpleNamespace(allow_legacy_unsigned_checkpoints=False)
    saver._secret_key = b"test-key"
    assert saver._verify_and_deserialize(pickle.dumps({"unsafe": True})) is None


@pytest.mark.asyncio
async def test_thread_id_cannot_be_claimed_by_another_principal() -> None:
    registry = ThreadRegistry()
    registry._client = AsyncMock(return_value=None)  # type: ignore[method-assign]
    first = await registry.claim("public-thread", "alice")
    assert first != "public-thread"
    with pytest.raises(ThreadOwnershipError):
        await registry.claim("public-thread", "bob")


@pytest.mark.asyncio
async def test_high_risk_tool_requires_step_scoped_approval() -> None:
    manager = MCPClientManager()

    async def harmless_write(**kwargs: object) -> str:
        return "ok"

    manager._local["repo_write_file"] = harmless_write
    tool = manager.get_tool("repo_write_file")
    assert tool is not None

    with pytest.raises(PermissionError, match="requires step-scoped HITL approval"):
        await tool.ainvoke({"file_path": "safe.txt", "content": "hello"})

    token = set_tool_execution_context(
        ToolExecutionContext(
            agent_name="coding_agent",
            step_id="step_1",
            hitl_approved=True,
            allowed_tools=("repo_write_file",),
        )
    )
    try:
        assert await tool.ainvoke({"file_path": "safe.txt", "content": "hello"}) == "ok"
    finally:
        reset_tool_execution_context(token)
