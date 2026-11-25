"""Regression tests for the 2026-11-28 independent readiness review."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from src.agentic.api.routes.chat import chat_stream_endpoint
from src.agentic.core.config import Settings
from src.agentic.core.schemas import ChatRequest
from src.agentic.orchestrator.nodes.reflector import ReflectionEvaluation, reflector_node
from src.agentic.orchestrator.state import AgentState, Plan, Step
from src.agentic.safety.audit import compute_audit_integrity_hash
from src.agentic.safety.guardrails import GuardrailManager

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_sse_fast_finalize_emits_answer_and_final_event() -> None:
    async def events(*_args: object, **_kwargs: object) -> AsyncIterator[dict[str, Any]]:
        yield {
            "event": "on_chain_end",
            "metadata": {"langgraph_node": "fast_finalize"},
            "data": {"output": {"final_answer": "Fast-path answer"}},
        }

    graph = MagicMock()
    graph.astream_events = events
    graph.aget_state = AsyncMock(return_value=SimpleNamespace(next=(), tasks=[]))
    response = await chat_stream_endpoint(
        ChatRequest(query="hello", thread_id="fast-path-thread", stream=True),
        graph=graph,
        guardrails=GuardrailManager(),
        user={"user_id": "review-user", "tenant_id": "public", "roles": ["user"]},
    )

    emitted = [
        event
        async for event in cast(AsyncIterator[dict[str, Any]], response.body_iterator)
    ]
    finals = [json.loads(event["data"]) for event in emitted if event["event"] == "final"]
    assert finals and finals[0]["final_answer"] == "Fast-path answer"
    assert any(
        event["event"] == "token"
        and json.loads(event["data"])["token"] == "Fast-path answer"
        for event in emitted
    )


def test_k8s_serving_has_all_alias_backends_and_mounted_litellm_config() -> None:
    docs = list(yaml.safe_load_all((ROOT / "deploy/k8s/serving.yaml").read_text("utf-8")))
    resources = {
        (doc.get("kind"), doc.get("metadata", {}).get("name")): doc
        for doc in docs
        if isinstance(doc, dict)
    }
    for workload in ("vllm", "vllm-coding", "llama-fast", "llama-chat-backup", "litellm"):
        assert ("Deployment", workload) in resources

    vllm = resources[("Deployment", "vllm")]["spec"]["template"]["spec"]["containers"][0]
    args = vllm["args"]
    assert "--enable-auto-tool-choice" in args
    assert "--tool-call-parser" in args
    assert "--enable-prefix-caching" in args
    assert vllm["resources"]["limits"]["nvidia.com/gpu"] >= 1

    litellm = resources[("Deployment", "litellm")]["spec"]["template"]["spec"]["containers"][0]
    assert "--config" in litellm["args"]
    assert any(mount["mountPath"] == "/app/config.yaml" for mount in litellm["volumeMounts"])

    cfg = yaml.safe_load((ROOT / "configs/litellm_config.yaml").read_text("utf-8"))
    assert all(
        "localhost" not in model["litellm_params"]["api_base"]
        for model in cfg["model_list"]
    )


@pytest.mark.asyncio
async def test_reflector_judge_receives_complete_bounded_evidence() -> None:
    tail = "TAIL_EVIDENCE_MUST_REACH_JUDGE"
    output = "x" * 400 + tail
    plan = Plan(
        goal="Verify evidence",
        steps=[Step(id="s1", description="work", agent="chat", input_query="q", status="completed", output=output)],
    )
    judge = MagicMock()
    structured = MagicMock()
    structured.ainvoke = AsyncMock(
        return_value=ReflectionEvaluation(
            passed=True, confidence_score=0.95, critique="ok", action="proceed"
        )
    )
    judge.with_structured_output.return_value = structured

    with patch("src.agentic.orchestrator.nodes.reflector.get_llm", return_value=judge):
        await reflector_node(AgentState(plan=plan, messages=[]))

    prompt = structured.ainvoke.await_args.args[0]
    assert tail in prompt


def test_audit_hmac_covers_trace_and_risk() -> None:
    common: dict[str, Any] = {
        "event_id": "evt",
        "event_type": "hitl",
        "actor_id": "alice",
        "thread_id": "thread",
        "step_id": "step",
        "decision": "approved",
        "details": {},
        "created_at": "2026-11-28T00:00:00+00:00",
        "signing_key": "secret",
    }
    base = compute_audit_integrity_hash(**common, trace_id="trace-a", risk_level="high")
    assert base != compute_audit_integrity_hash(**common, trace_id="trace-b", risk_level="high")
    assert base != compute_audit_integrity_hash(**common, trace_id="trace-a", risk_level="low")


def test_strict_config_rejects_symmetric_jwt_and_loopback_cors() -> None:
    common: dict[str, Any] = {
        "environment": "production",
        "service_role": "api",
        "jwt_secret_key": "real-jwt-secret",
        "s2s_internal_token": "real-s2s-secret",
        "checkpoint_hmac_secret": "real-checkpoint-secret",
        "approval_private_key": "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE=",
        "litellm_api_key": "real-litellm-key",
        "postgres_uri": "postgresql://app:secret@postgres-svc/agentic",
        "use_postgres_checkpoint": True,
        "use_remote_mcp": True,
        "require_authentication": True,
    }
    with pytest.raises(ValueError, match="symmetric JWT"):
        Settings(**common, jwt_allowed_algorithms=["HS256"], allowed_origins=["https://app.example"])
    with pytest.raises(ValueError, match="loopback"):
        Settings(**common, jwt_allowed_algorithms=["RS256"], allowed_origins=["http://localhost:3000"])
