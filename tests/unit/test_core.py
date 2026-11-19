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
