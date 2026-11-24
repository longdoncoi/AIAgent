"""Regression tests for cross-boundary authorization and state integrity."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from src.agentic.memory.redis_saver import RedisSaver
from src.agentic.memory.thread_registry import (
    ThreadOwnershipError,
    ThreadRegistry,
    ThreadRegistryUnavailable,
)
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
async def test_thread_registry_test_mode_never_uses_external_redis() -> None:
    registry = ThreadRegistry()
    with patch("redis.asyncio.from_url") as from_url:
        assert await registry._client() is None
    from_url.assert_not_called()


@pytest.mark.asyncio
async def test_thread_registry_redis_operations_fail_closed_in_production() -> None:
    registry = ThreadRegistry()
    registry.settings = SimpleNamespace(
        environment="production",
        redis_url="redis://unused",
        checkpoint_signing_key="test-checkpoint-key",
    )
    client = AsyncMock()
    registry._client = AsyncMock(return_value=client)  # type: ignore[method-assign]

    client.set.side_effect = RuntimeError("redis claim failed")
    with pytest.raises(ThreadRegistryUnavailable, match="claim"):
        await registry.claim("thread", "alice")

    client.reset_mock()
    client.get.side_effect = RuntimeError("redis owner failed")
    with pytest.raises(ThreadRegistryUnavailable, match="owner"):
        await registry.owner("thread")

    client.reset_mock()
    client.smembers.side_effect = RuntimeError("redis index failed")
    with pytest.raises(ThreadRegistryUnavailable, match="owner index"):
        await registry.threads_for_owner("alice")


@pytest.mark.asyncio
async def test_high_risk_tool_requires_step_scoped_approval() -> None:
    from unittest.mock import patch

    from src.agentic.safety.approvals import issue_approval_grant

    manager = MCPClientManager()

    async def harmless_write(**kwargs: object) -> str:
        return "ok"

    manager._local["repo_write_file"] = harmless_write
    tool = manager.get_tool("repo_write_file")
    assert tool is not None

    # Force the local implementation: no MCP server is running in unit tests.
    with patch.object(manager.settings, "use_remote_mcp", False):
        with pytest.raises(PermissionError, match="requires step-scoped HITL approval"):
            await tool.ainvoke({"file_path": "safe.txt", "content": "hello"})

        # A caller-supplied boolean can no longer authorize the tool.
        forged = set_tool_execution_context(
            ToolExecutionContext(
                agent_name="coding_agent",
                step_id="step_1",
                approval_token="forged.token",
                step_fingerprint="fp",
            )
        )
        try:
            with pytest.raises(PermissionError, match="requires step-scoped HITL approval"):
                await tool.ainvoke({"file_path": "safe.txt", "content": "hello"})
        finally:
            reset_tool_execution_context(forged)

        grant = issue_approval_grant(
            step_id="step_1",
            fingerprint="fp",
            owner_id="alice",
            agent_name="coding_agent",
            tools=["repo_write_file"],
        )
        token = set_tool_execution_context(
            ToolExecutionContext(
                agent_name="coding_agent",
                step_id="step_1",
                approval_token=grant,
                owner_id="alice",
                step_fingerprint="fp",
            )
        )
        try:
            assert await tool.ainvoke({"file_path": "safe.txt", "content": "hello"}) == "ok"
        finally:
            reset_tool_execution_context(token)
