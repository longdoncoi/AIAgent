"""Unit tests for core schemas, errors, and configuration."""

from src.agentic.core.config import get_settings
from src.agentic.core.errors import AgentError, GuardrailViolation, HITLRequiredError
from src.agentic.core.schemas import ChatRequest, ChatResponse, Decision, StepResult
from src.agentic.safety.policy import get_policy_manager


def test_settings_loading():
    settings = get_settings()
    assert settings.host == "0.0.0.0"
    assert settings.port == 8000
    assert settings.default_reasoning_model == "reasoning-llm"
    assert settings.policies_path.name == "policies.yaml"


def test_custom_errors():
    err = AgentError("General failure", details={"code": 500})
    assert str(err) == "General failure"
    assert err.details["code"] == 500

    gv = GuardrailViolation("PII detected", rule="pii_defense")
    assert gv.rule == "pii_defense"

    hitl = HITLRequiredError("step_1", "repo_write_file", "high")
    assert hitl.step_id == "step_1"
    assert hitl.risk_level == "high"


def test_core_schemas():
    req = ChatRequest(query="Xin chào", thread_id="t_123")
    assert req.query == "Xin chào"
    assert req.thread_id == "t_123"

    step = StepResult(step_id="step_1", agent="chat", output="Hello", status="completed")
    res = ChatResponse(thread_id="t_123", answer="Hello", steps=[step])
    assert res.thread_id == "t_123"
    assert len(res.steps) == 1
    assert res.steps[0].output == "Hello"

    dec = Decision(approved=True, reason="Acceptable risk")
    assert dec.approved is True


def test_policy_manager():
    policy = get_policy_manager()
    assert policy.get_tool_risk("web_search") == "low"
    assert policy.get_tool_risk("repo_write_file") == "high"
    assert policy.is_approval_required("repo_write_file") is True
    assert policy.is_approval_required("web_search") is False

    allowed = policy.get_agent_tools("coding_agent")
    assert "repo_write_file" in allowed
    assert "code_exec_python" in allowed


def test_singleton_redis_connection_pools():
    from unittest.mock import MagicMock, patch

    from src.agentic.core.redis import (
        close_redis_pools,
        get_async_redis_client,
        get_async_redis_connection_pool,
        get_redis_client,
        get_redis_connection_pool,
    )

    mock_pool = MagicMock()
    mock_async_pool = MagicMock()

    with patch("redis.ConnectionPool.from_url", return_value=mock_pool):
        with patch("redis.asyncio.ConnectionPool.from_url", return_value=mock_async_pool):
            close_redis_pools()
            p1 = get_redis_connection_pool()
            p2 = get_redis_connection_pool()
            assert p1 == p2

            ap1 = get_async_redis_connection_pool()
            ap2 = get_async_redis_connection_pool()
            assert ap1 == ap2

            with patch("redis.Redis", return_value=MagicMock()) as mock_redis_cls:
                c1 = get_redis_client()
                assert c1 is not None
                mock_redis_cls.assert_called_once_with(connection_pool=p1)

            with patch("redis.asyncio.Redis", return_value=MagicMock()) as mock_aredis_cls:
                ac1 = get_async_redis_client()
                assert ac1 is not None
                mock_aredis_cls.assert_called_once_with(connection_pool=ap1)

            close_redis_pools()
            mock_pool.disconnect.assert_called_once()

