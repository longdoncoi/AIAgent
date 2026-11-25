"""Comprehensive regression tests covering all PRR Review P0, P1, and P2 remediations."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx2
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult, LLMResult
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from llama_index.core.schema import NodeWithScore, TextNode

from scripts.restore_drill import run_sqlite_restore_smoke
from src.agentic.a2a.cards import AgentCard
from src.agentic.a2a.client import A2AClient
from src.agentic.a2a.executor import A2AAgentExecutor, A2ATaskPayload, A2ATaskRequest
from src.agentic.agents.base import build_react_graph
from src.agentic.api.deps import get_current_user
from src.agentic.cache.rag_cache import RAGCache
from src.agentic.cache.redaction import redact_pii
from src.agentic.core.config import Settings
from src.agentic.core.errors import ToolCallApprovalRequired
from src.agentic.core.execution_context import (
    ToolExecutionContext,
    reset_tool_execution_context,
    set_tool_execution_context,
)
from src.agentic.llm.gateway import TokenMetricsCallbackHandler, _circuit_breaker
from src.agentic.memory.data_lifecycle import purge_user_data
from src.agentic.memory.redis_saver import RedisSaver
from src.agentic.memory.thread_registry import ThreadRegistry
from src.agentic.observability.metrics import (
    HITL_PENDING_APPROVALS,
    MODEL_PROXY_FALLBACK_TOTAL,
)
from src.agentic.orchestrator.graph import create_supervisor_graph
from src.agentic.orchestrator.nodes.planner import Plan, Step, _apply_risk_policy
from src.agentic.orchestrator.state import build_turn_input
from src.agentic.rag.embedding import configure_embedding_model, invalidate_embedding_provider_cache
from src.agentic.rag.ingestion import ingest_documents
from src.agentic.rag.retriever import HybridRetriever
from src.agentic.rag.tools import (
    format_citations,
    search_policy,
    validate_citations,
)
from src.agentic.safety.approvals import _b64encode, issue_approval_grant, verify_approval_grant
from src.agentic.safety.audit import record_audit_event, verify_audit_chain
from src.agentic.safety.guardrails import GuardrailManager
from src.agentic.tools.mcp_client import MCPClientManager

ROOT = Path(__file__).resolve().parents[2]


def _valid_strict_kwargs(**overrides: Any) -> dict[str, Any]:
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    base: dict[str, Any] = {
        "environment": "staging",
        "service_role": "all",
        "jwt_secret_key": "staging-jwt-secret-key-unique-32-chars",
        "jwt_algorithm": "RS256",
        "jwt_allowed_algorithms": ["RS256"],
        "jwt_jwks_url": "https://auth.example.com/.well-known/jwks.json",
        "s2s_internal_token": "staging-s2s-internal-token-32-chars",
        "checkpoint_hmac_secret": "staging-hmac-secret-distinct-32-chars",
        "approval_private_key": _b64encode(priv.private_bytes_raw()),
        "approval_public_key": _b64encode(pub.public_bytes_raw()),
        "litellm_api_key": "sk-staging-real-key",
        "neo4j_password": "staging-neo4j-password",
        "postgres_uri": "postgresql://app:real@pgbouncer-svc:6432/agentic_db",
        "require_authentication": True,
        "use_postgres_checkpoint": True,
        "enable_qdrant": False,
        "fast_heuristic_planner": False,
        "allowed_origins": ["https://staging.example.internal"],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# P0-1: Cross-tenant / cross-user / cross-thread idempotency isolation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p0_1_idempotency_never_leaks_across_tenants_users_or_threads() -> None:
    calls = 0

    class _EchoGraph:
        async def ainvoke(self, state: dict[str, Any]) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            from src.agentic.core.execution_context import get_tool_execution_context

            ctx = get_tool_execution_context()
            tenant = ctx.tenant_id if ctx else "none"
            owner = ctx.owner_id if ctx else "none"
            return {"messages": [AIMessage(content=f"tenant={tenant}:owner={owner}:call={calls}")]}

    executor = A2AAgentExecutor(
        card=AgentCard(name="rag_agent", description="RAG", url="http://localhost:9101", skills=[]),
        graph=_EchoGraph(),
    )

    # Simulate an attacker/caller supplying identical step_id & identical idempotency_key across tenants.
    req_tenant_a = A2ATaskRequest(
        task_id="step_1",
        target_agent="rag_agent",
        payload=A2ATaskPayload(instruction="Chính sách MFA là gì?"),
        metadata={"tenant_id": "tenant-a", "owner_id": "alice", "thread_id": "thr-1", "turn_id": "u1"},
        idempotency_key="shared-fingerprint-collision",
    )
    req_tenant_b = A2ATaskRequest(
        task_id="step_1",
        target_agent="rag_agent",
        payload=A2ATaskPayload(instruction="Chính sách MFA là gì?"),
        metadata={"tenant_id": "tenant-b", "owner_id": "bob", "thread_id": "thr-2", "turn_id": "u2"},
        idempotency_key="shared-fingerprint-collision",
    )
    req_same_tenant_other_user = A2ATaskRequest(
        task_id="step_1",
        target_agent="rag_agent",
        payload=A2ATaskPayload(instruction="Chính sách MFA là gì?"),
        metadata={"tenant_id": "tenant-a", "owner_id": "charlie", "thread_id": "thr-3", "turn_id": "u3"},
        idempotency_key="shared-fingerprint-collision",
    )

    res_a1 = await executor.execute(req_tenant_a)
    res_a2 = await executor.execute(req_tenant_a)  # Genuine retry in exact same tenant/owner/thread/turn
    res_b = await executor.execute(req_tenant_b)
    res_c = await executor.execute(req_same_tenant_other_user)

    assert calls == 3
    assert "tenant=tenant-a:owner=alice" in res_a1.output
    assert res_a2.output == res_a1.output
    assert "tenant=tenant-b:owner=bob" in res_b.output
    assert "tenant=tenant-a:owner=charlie" in res_c.output

    # Verify streaming endpoint applies the same server-side tenant/owner/thread scoping
    stream_b = [ev async for ev in executor.execute_stream(req_tenant_b)]
    assert any(ev.get("event") == "completed" and "tenant-b:owner=bob" in ev.get("output", "") for ev in stream_b)

    # Verify A2AClient derives distinct idempotency keys even when metadata omits idempotency_key
    client = A2AClient()
    client.settings = Settings(environment="test", allow_local_agent_fallback=True, a2a_chat_agent_url="")
    seen_keys: list[str] = []

    async def _cap_post(url: str, *, json: dict[str, Any], headers: dict[str, Any], timeout: float) -> Any:
        seen_keys.append(str(json.get("idempotency_key")))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"task_id": "step_1", "status": "completed", "output": "ok", "error": None},
        )

    client._endpoints = {"rag": "http://mock-rag"}
    client._get_http_client = lambda timeout=60.0: SimpleNamespace(post=_cap_post)  # type: ignore[method-assign]
    await client.call_agent("rag", "step_1", "MFA?", metadata={"tenant_id": "t1", "owner_id": "u1", "thread_id": "th1", "step_fingerprint": "same"})
    await client.call_agent("rag", "step_1", "MFA?", metadata={"tenant_id": "t2", "owner_id": "u1", "thread_id": "th1", "step_fingerprint": "same"})
    assert len(seen_keys) == 2 and seen_keys[0] != seen_keys[1]


# ---------------------------------------------------------------------------
# P0-2: Multi-turn conversation resets reflection/degraded/HITL state cleanly
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p0_2_multi_turn_state_resets_every_turn_and_never_skips_reflector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAST_HEURISTIC_PLANNER", "false")
    planner_prompts_seen: list[str] = []
    reflector_calls = 0

    class _MultiTurnPlannerLLM:
        def with_structured_output(self, schema: Any) -> Any:
            async def _ainvoke(messages: list[Any]) -> Plan:
                planner_prompts_seen.append(" ".join(str(getattr(m, "content", "")) for m in messages))
                # Use rag step so both Reflector and Finalize execute on every turn
                return Plan(
                    goal="Policy Q",
                    steps=[
                        Step(
                            id="step_1",
                            description="Query policy",
                            agent="rag",
                            input_query=f"turn query #{len(planner_prompts_seen)}",
                            risk_level="low",
                        )
                    ],
                )

            return SimpleNamespace(ainvoke=_ainvoke)

    class _JudgeLLM:
        def with_structured_output(self, schema: Any) -> Any:
            async def _ainvoke(_p: Any) -> Any:
                nonlocal reflector_calls
                reflector_calls += 1
                from src.agentic.orchestrator.nodes.reflector import ReflectionEvaluation

                return ReflectionEvaluation(
                    passed=True, confidence_score=0.95, critique="Verified ok", action="proceed"
                )

            return SimpleNamespace(ainvoke=_ainvoke)

    class _SynthLLM:
        async def ainvoke(self, _p: Any) -> AIMessage:
            return AIMessage(content="Synthesized grounded answer [step_1].")

    class _FakeA2A:
        async def call_agent_stream(self, **kwargs: Any):  # type: ignore[no-untyped-def]
            cites = format_citations([{"doc_id": "d1", "file_name": "security_policy.md"}])
            out = f"Policy states MFA required.\n\n{cites}"
            yield {"event": "completed", "output": out}

    graph = create_supervisor_graph(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "thread-multi-turn-1"}}

    with (
        patch("src.agentic.orchestrator.nodes.planner.get_llm", return_value=_MultiTurnPlannerLLM()),
        patch("src.agentic.orchestrator.nodes.reflector.get_llm", return_value=_JudgeLLM()),
        patch("src.agentic.orchestrator.nodes.finalize.get_llm", return_value=_SynthLLM()),
        patch("src.agentic.orchestrator.nodes.dispatch.get_a2a_client", return_value=_FakeA2A()),
    ):
        for turn_num in range(1, 5):
            if turn_num % 2 == 1:
                inp = build_turn_input(
                    user_query=f"Turn {turn_num} policy question",
                    thread_id="thread-multi-turn-1",
                    metadata={"user_id": "alice", "tenant_id": "org-a"},
                )
            else:
                # Direct HumanMessage invocation without build_turn_input must also isolate turns!
                inp = {"messages": [HumanMessage(content=f"Turn {turn_num} follow-up")]}
            final = await graph.ainvoke(inp, config=config)
            assert final.get("final_answer")
            # Turn-scoped count after 1 reflection pass must always be 1 (never 2, 3, 4)
            assert final["reflection_count"] == 1

    assert len(planner_prompts_seen) == 4
    assert reflector_calls == 4  # Every turn including Turn 3 and Turn 4 invoked Reflector!
    assert all("Previous reflection feedback" not in p for p in planner_prompts_seen[1:])


# ---------------------------------------------------------------------------
# P0-3: Staging enforces zero-trust inviolables & default Settings fail-closed
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p0_3_staging_inviolables_and_fail_closed_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REQUIRE_AUTHENTICATION", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    # Fail-closed default outside test overrides
    default_settings = Settings(_env_file=None, environment="development")
    assert default_settings.require_authentication is True

    # Staging rejects default insecure jwt_secret_key, fast_heuristic_planner, and wildcard CORS
    with pytest.raises(ValueError, match="jwt_secret_key"):
        Settings(**_valid_strict_kwargs(jwt_secret_key="agentic-secret-key-change-in-production-2026"))
    with pytest.raises(ValueError, match="fast_heuristic_planner"):
        Settings(**_valid_strict_kwargs(fast_heuristic_planner=True))
    with pytest.raises(ValueError, match="allowed_origins"):
        Settings(**_valid_strict_kwargs(allowed_origins=["*"]))

    valid_staging = Settings(**_valid_strict_kwargs())
    assert valid_staging.is_strict_environment is True
    assert valid_staging.is_production is False

    # Development environment must reject hardcoded test tokens ("admin_token", "test_token", "secret")
    dev_settings = Settings(environment="development", require_authentication=True)
    with patch("src.agentic.api.deps.get_settings", return_value=dev_settings):
        for tok in ("admin_token", "test_token", "secret"):
            with pytest.raises(Exception) as exc_info:
                await get_current_user(authorization=f"Bearer {tok}")
            assert getattr(exc_info.value, "status_code", None) == 401


# ---------------------------------------------------------------------------
# P1-1: Real LangGraph HITL pause + resume leaves zero gauge leak
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p1_1_real_langgraph_hitl_pause_and_resume_zero_gauge_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAST_HEURISTIC_PLANNER", "true")
    graph = create_supervisor_graph(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "thr-real-hitl-gauge"}}

    class _FakeA2A:
        async def call_agent_stream(self, **kwargs: Any):  # type: ignore[no-untyped-def]
            yield {"event": "completed", "output": "File written safely."}

    class _JudgeLLM:
        def with_structured_output(self, schema: Any) -> Any:
            async def _ainvoke(_p: Any) -> Any:
                from src.agentic.orchestrator.nodes.reflector import ReflectionEvaluation

                return ReflectionEvaluation(passed=True, confidence_score=0.9, critique="OK", action="proceed")

            return SimpleNamespace(ainvoke=_ainvoke)

    class _SynthLLM:
        async def ainvoke(self, _p: Any) -> AIMessage:
            return AIMessage(content="Completed [step_1].")

    before = HITL_PENDING_APPROVALS._value.get()
    with patch("src.agentic.orchestrator.nodes.hitl.record_audit_event", return_value="ev-1"):
        await graph.ainvoke(
            build_turn_input(
                user_query="Write and overwrite configs/sample.yaml file",
                thread_id="thr-real-hitl-gauge",
                metadata={"user_id": "alice", "tenant_id": "org-a"},
            ),
            config=config,
        )
        assert HITL_PENDING_APPROVALS._value.get() == before + 1

        with (
            patch("src.agentic.orchestrator.nodes.dispatch.get_a2a_client", return_value=_FakeA2A()),
            patch("src.agentic.orchestrator.nodes.reflector.get_llm", return_value=_JudgeLLM()),
            patch("src.agentic.orchestrator.nodes.finalize.get_llm", return_value=_SynthLLM()),
        ):
            resumed = await graph.ainvoke(
                Command(resume={"approved": True, "approver_id": "supervisor-bob"}),
                config=config,
            )
    assert resumed.get("final_answer")
    assert HITL_PENDING_APPROVALS._value.get() == before


# ---------------------------------------------------------------------------
# P1-2, P1-3, P1-17: Two-phase approval for code_exec_python via real create_react_agent + httpx
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p1_2_3_17_two_phase_python_via_real_react_graph_and_httpx_streamable() -> None:
    test_settings = Settings(environment="test", require_tool_call_approval=True, use_remote_mcp=False)
    mgr = MCPClientManager()
    mgr.settings = test_settings
    py_tool = next(t for t in mgr.get_tools_for_agent("coding_agent") if t.name == "code_exec_python")

    class _ToolCallingModel(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "fake-tool-caller"

        def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "code_exec_python",
                                    "args": {"code": "print('hi')"},
                                    "id": "call-py-1",
                                }
                            ],
                        )
                    )
                ]
            )

        def bind_tools(self, tools: Any, **kwargs: Any) -> _ToolCallingModel:
            return self

    react_graph = build_react_graph(model=_ToolCallingModel(), tools=[py_tool], prompt="coding")

    step = Step(
        id="step_1",
        description="Execute python code",
        agent="coding",
        input_query="Run print('hi')",
        risk_level="high",
    )
    from src.agentic.orchestrator.nodes.hitl import compute_step_fingerprint

    fp = compute_step_fingerprint(step)
    token = issue_approval_grant(
        step_id="step_1",
        fingerprint=fp,
        owner_id="alice",
        agent_name="coding_agent",
        approved_args_digests=None,  # Step-approved only; no specific tool call approved yet!
        tools=["code_exec_python"],
    )
    ctx_tok = set_tool_execution_context(
        ToolExecutionContext(
            agent_name="coding_agent",
            step_id="step_1",
            approval_token=token,
            owner_id="alice",
            step_fingerprint=fp,
        )
    )
    try:
        # Must raise ToolCallApprovalRequired out of real ToolNode/create_react_agent without swallowing
        with pytest.raises(ToolCallApprovalRequired) as exc_info:
            await react_graph.ainvoke({"messages": [HumanMessage(content="run python")]})
        assert exc_info.value.tool_name == "code_exec_python"
        assert exc_info.value.arguments == {"code": "print('hi')", "timeout": 30}
    finally:
        reset_tool_execution_context(ctx_tok)

    # P1-17: _remote_call uses declared `httpx2` and pyproject.toml declares it explicitly
    assert "httpx2>=" in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    mgr.settings = Settings(environment="test", mcp_transport="streamable-http", use_remote_mcp=True)
    mock_session = AsyncMock()
    mock_session.call_tool = AsyncMock(return_value=SimpleNamespace(isError=False, content=[SimpleNamespace(text="ok")]))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    class _StreamCM:
        async def __aenter__(self) -> tuple[Any, Any, None]:
            return MagicMock(), MagicMock(), None

        async def __aexit__(self, *args: Any) -> None:
            return None

    class _AsyncClientCM:
        def __init__(self, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> _AsyncClientCM:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

    with (
        patch.object(httpx2, "AsyncClient", _AsyncClientCM),
        patch("mcp.client.streamable_http.streamable_http_client", return_value=_StreamCM()),
        patch("mcp.ClientSession", return_value=mock_session),
    ):
        remote_out = await mgr._remote_call("repo_read_file", {"file_path": "README.md"}, timeout=5.0)
        assert remote_out == "ok"


# ---------------------------------------------------------------------------
# P1-4: Non-coding keyword queries don't trigger HITL approval fatigue
# ---------------------------------------------------------------------------
def test_p1_4_non_coding_queries_remain_low_risk() -> None:
    plan = Plan(
        goal="Help user",
        steps=[
            Step(id="s1", description="Answer ticket workflow", agent="chat", input_query="How do I create a ticket?"),
            Step(id="s2", description="Lookup password policy", agent="rag", input_query="Chính sách tạo mật khẩu mới là gì?"),
            Step(id="s3", description="Run shell check", agent="coding", input_query="Run check via bash"),
        ],
    )
    _apply_risk_policy(plan)
    assert plan.steps[0].risk_level == "low"
    assert plan.steps[1].risk_level == "low"
    assert plan.steps[2].risk_level == "high"


# ---------------------------------------------------------------------------
# P1-5, P1-6, P1-7: Lazy cache embedder, numeric guard, shared scope, redaction parity
# ---------------------------------------------------------------------------
def test_p1_5_6_7_rag_cache_hardening_and_shared_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = RAGCache(max_entries=50, default_ttl=300, enable_redis=False)
    # 1. Numeric / quantifier mismatch never hits semantic cache even with identical vectors
    v = [1.0, 0.0, 0.0]
    cache.set_semantic("What is retention policy 2025?", "Policy 2025 is 30 days.", domain="general", top_k=3, query_vector=v)
    assert cache.get_semantic("What is retention policy 2026?", domain="general", top_k=3, query_vector=v) is None

    # 2. Strict env rejects fuzzy trigram cache matches across paraphrased text
    cache_trigram = RAGCache(max_entries=50, default_ttl=300, enable_redis=False)
    cache_trigram.set_semantic("travel reimbursement rules", "Reimburse economy tickets", domain="chat", top_k=3)
    with patch.object(cache_trigram, "settings", Settings(**_valid_strict_kwargs())):
        assert cache_trigram.get_semantic("what are our travel reimbursement rules", domain="chat", top_k=3) is None

    # 3. Late configure_embedding_model upgrades existing RAGCache singleton to neural embeddings
    from llama_index.core import Settings as LlamaSettings
    from llama_index.core.embeddings import BaseEmbedding

    class _FakeRealEmbedding(BaseEmbedding):
        model_name: str = "text-embedding-bge"
        dim: int = cache.settings.default_embedding_dim

        def _get_query_embedding(self, query: str) -> list[float]:
            return [0.25] * self.dim

        async def _aget_query_embedding(self, query: str) -> list[float]:
            return [0.25] * self.dim

        def _get_text_embedding(self, text: str) -> list[float]:
            return [0.25] * self.dim

        async def _aget_text_embedding(self, text: str) -> list[float]:
            return [0.25] * self.dim

    fake_real = _FakeRealEmbedding()
    singleton = RAGCache(enable_redis=False)
    orig_embed = LlamaSettings._embed_model
    try:
        with (
            patch("src.agentic.cache.rag_cache.get_rag_cache", return_value=singleton),
            patch("src.agentic.rag.embedding.is_embedding_service_reachable", return_value=True),
            patch("llama_index.embeddings.openai.OpenAIEmbedding", return_value=fake_real),
        ):
            provider = configure_embedding_model(force=True)
        assert provider == "real"
        vec = singleton.get_embedding("test neural upgrade")
        assert len(vec) == cache.settings.default_embedding_dim
    finally:
        LlamaSettings._embed_model = orig_embed
        invalidate_embedding_provider_cache()

    # 4. Citation-preserving L1/L2 redaction parity + VN phone & CMND
    cites = format_citations([{"doc_id": "123456789", "file_name": "hr.md"}])
    raw = f"Call 0912 345 678 or email hr@acme.com for Order ID 987654321.\n\n{cites}"
    redacted = redact_pii(raw)
    assert "[REDACTED_PHONE]" in redacted
    assert "[REDACTED_EMAIL]" in redacted
    assert "Order ID 987654321" in redacted
    assert validate_citations(redacted)
    assert "0912 345 678" not in GuardrailManager().mask_pii("Call 0912 345 678")

    # 5. Cross-user shared cache reuse within same tenant/dept/role (P1-6)
    shared_node = NodeWithScore(
        node=TextNode(
            text="Shared security handbook",
            metadata={
                "doc_id": "d-shared",
                "file_name": "handbook.md",
                "tenant_id": "acme",
                "department": "engineering",
                "allowed_roles": ["user"],
                "acl": ["acme", "engineering", "role:user"],
            },
        ),
        score=0.95,
    )
    retrieve_calls = 0

    def _fake_retrieve(query: str, top_k: int = 6, filters: dict[str, Any] | None = None) -> list[NodeWithScore]:
        nonlocal retrieve_calls
        retrieve_calls += 1
        return [shared_node]

    fake_reranker = MagicMock(mode="heuristic")
    fake_reranker.rerank.side_effect = lambda _q, nodes: nodes
    shared_cache = RAGCache(max_entries=50, default_ttl=300, enable_redis=False)
    with (
        patch("src.agentic.rag.tools.get_rag_cache", return_value=shared_cache),
        patch("src.agentic.rag.tools.get_hybrid_retriever", return_value=SimpleNamespace(retrieve=_fake_retrieve)),
        patch("src.agentic.rag.tools.CrossEncoderReranker", return_value=fake_reranker),
    ):
        t_alice = set_tool_execution_context(
            ToolExecutionContext(
                agent_name="rag", step_id="s1", owner_id="alice", tenant_id="acme",
                user_roles=("user",), department="engineering",
            )
        )
        try:
            r1 = search_policy("handbook summary")
        finally:
            reset_tool_execution_context(t_alice)

        t_bob = set_tool_execution_context(
            ToolExecutionContext(
                agent_name="rag", step_id="s2", owner_id="bob", tenant_id="acme",
                user_roles=("user",), department="engineering",
            )
        )
        try:
            r2 = search_policy("handbook summary")
        finally:
            reset_tool_execution_context(t_bob)

    assert retrieve_calls == 1
    assert r1 == r2 and "Shared security handbook" in r2


# ---------------------------------------------------------------------------
# P1-8: Multi-tenant ingestion (`data/raw/tenants/<id>`) & public corpus visibility
# ---------------------------------------------------------------------------
def test_p1_8_multi_tenant_ingestion_and_optional_public_corpus(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    (raw / "tenants" / "acme").mkdir(parents=True)
    (raw / "tenants" / "beta").mkdir(parents=True)
    (raw / "public_base.md").write_text("Company baseline guideline. " * 25, encoding="utf-8")
    (raw / "tenants" / "acme" / "acme.md").write_text("Acme private runbook. " * 25, encoding="utf-8")
    (raw / "tenants" / "beta" / "beta.md").write_text("Beta private budget. " * 25, encoding="utf-8")

    with (
        patch("src.agentic.rag.retriever.get_qdrant_vector_store", return_value=None),
        patch("src.agentic.rag.kg.get_knowledge_graph_store"),
    ):
        count = ingest_documents(raw_data_dir=raw, index_persist_dir=tmp_path / "index")
    assert count >= 3
    docstore = json.loads((tmp_path / "index" / "docstore.json").read_text(encoding="utf-8"))
    tenants = {
        entry["__data__"]["metadata"]["tenant_id"]
        for entry in docstore["docstore/data"].values()
    }
    assert tenants == {"public", "acme", "beta"}

    retriever = HybridRetriever.__new__(HybridRetriever)
    retriever.settings = Settings(environment="test", enforce_rag_rbac=True, rag_include_public_corpus=True)
    pub_node = NodeWithScore(node=TextNode(text="pub", metadata={"tenant_id": "public"}), score=1.0)
    beta_node = NodeWithScore(node=TextNode(text="beta", metadata={"tenant_id": "beta"}), score=1.0)
    assert retriever._is_permitted(pub_node, {"tenant_id": "acme"}) is True
    assert retriever._is_permitted(beta_node, {"tenant_id": "acme"}) is False


# ---------------------------------------------------------------------------
# P1-9: Server-side LiteLLM fallback detection metric + breaker + alert rule
# ---------------------------------------------------------------------------
def test_p1_9_litellm_server_side_fallback_observed() -> None:
    before = MODEL_PROXY_FALLBACK_TOTAL.labels(
        requested_alias="reasoning-llm", observed_model="openai/llama-server"
    )._value.get()
    cb = TokenMetricsCallbackHandler("reasoning-llm")
    _circuit_breaker.record_success("reasoning-llm")
    for _ in range(3):
        cb._record_success(
            LLMResult(generations=[], llm_output={"model_name": "openai/llama-server", "token_usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        )
    after = MODEL_PROXY_FALLBACK_TOTAL.labels(
        requested_alias="reasoning-llm", observed_model="openai/llama-server"
    )._value.get()
    assert after == before + 3
    assert _circuit_breaker.should_fallback("reasoning-llm") is True
    _circuit_breaker.record_success("reasoning-llm")

    alerts_text = (ROOT / "deploy" / "monitoring" / "alerts.yaml").read_text(encoding="utf-8")
    assert "agent_model_proxy_fallback_total" in alerts_text


# ---------------------------------------------------------------------------
# P1-11, P1-12, P2-3, P2-6: Audit actor_id, tamper verify_audit_chain, restore drill & key rotation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p1_11_12_p2_3_6_audit_chain_rtbf_restore_and_key_rotation(tmp_path: Path) -> None:
    db_path = tmp_path / "audit.db"
    old_secret = "old-hmac-secret-at-least-32-bytes-long"
    new_secret = "new-hmac-secret-at-least-32-bytes-long"
    cfg_old = Settings(environment="test", checkpoint_hmac_secret=old_secret)

    class _InMemRedis:
        def __init__(self) -> None:
            self.keys: set[str] = {"agentic:checkpoint:thr-alice"}

        def scan_iter(self, match: str = "*", count: int = 200) -> list[str]:
            return [k for k in sorted(self.keys) if "alice" in k]

        def delete(self, *keys: str) -> int:
            deleted = 0
            for k in keys:
                if k in self.keys:
                    self.keys.remove(k)
                    deleted += 1
            return deleted

        def srem(self, _key: str, _val: str) -> int:
            return 0

    fake_redis = _InMemRedis()

    # Record RTBF audit with admin actor_id while targeting alice
    with (
        patch("src.agentic.memory.data_lifecycle.record_audit_event", side_effect=lambda **kw: record_audit_event(**kw, settings=cfg_old, sqlite_path=db_path)),
        patch("src.agentic.memory.data_lifecycle._default_cache_redis_client", return_value=None),
    ):
        report = await purge_user_data(
            "alice",
            actor_id="sec-admin-1",
            pg_conn=None,
            redis_client=fake_redis,
            long_term_store=SimpleNamespace(list_by_user=lambda _u: [], delete_by_user=lambda _u: 0),
            rag_cache=SimpleNamespace(invalidate_by_source=lambda _s: 0),
        )
    assert report.status == "ok"
    assert not fake_redis.keys

    # Append second event after secret rotation and verify full chain across both keys
    cfg_rotated = Settings(
        environment="test",
        checkpoint_hmac_secret=new_secret,
        checkpoint_hmac_previous_secrets=[old_secret],
    )
    record_audit_event(
        event_type="hitl_decision",
        actor_id="bob",
        decision="approved",
        settings=cfg_rotated,
        sqlite_path=db_path,
    )
    valid, count, errors = verify_audit_chain(settings=cfg_rotated, sqlite_path=db_path)
    assert valid is True and count == 2 and errors == []

    # Verify RTBF logged sec-admin-1 as actor_id and alice as target_user_id
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute("SELECT actor_id, details FROM audit_logs ORDER BY rowid ASC").fetchall()
        assert rows[0][0] == "sec-admin-1"
        assert json.loads(rows[0][1])["target_user_id"] == "alice"
        # Tamper first row and ensure verify_audit_chain catches it!
        conn.execute("UPDATE audit_logs SET actor_id = 'attacker' WHERE rowid = 1")
        conn.commit()
    tampered_valid, _, tampered_errors = verify_audit_chain(settings=cfg_rotated, sqlite_path=db_path)
    assert tampered_valid is False and len(tampered_errors) >= 1

    # Key rotation: Ed25519 grant signed by old key validates under previous public keys, unknown kid fails
    priv1 = Ed25519PrivateKey.generate()
    pub1 = priv1.public_key()
    priv2 = Ed25519PrivateKey.generate()
    pub2 = priv2.public_key()
    issuing_cfg = Settings(
        environment="test",
        approval_private_key=_b64encode(priv1.private_bytes_raw()),
        approval_public_key=_b64encode(pub1.public_bytes_raw()),
        approval_key_id="kid-v1",
    )
    rotated_cfg = Settings(
        environment="test",
        approval_private_key=_b64encode(priv2.private_bytes_raw()),
        approval_public_key=_b64encode(pub2.public_bytes_raw()),
        approval_key_id="kid-v2",
        approval_previous_public_keys={"kid-v1": _b64encode(pub1.public_bytes_raw())},
    )
    grant = issue_approval_grant(
        step_id="s1", fingerprint="fp1", owner_id="alice", agent_name="coding_agent", settings=issuing_cfg
    )
    assert (
        verify_approval_grant(
            grant, step_id="s1", fingerprint="fp1", owner_id="alice", agent_name="coding_agent", settings=rotated_cfg
        )
        is not None
    )
    untrusted_cfg = Settings(
        environment="test",
        approval_private_key=_b64encode(priv2.private_bytes_raw()),
        approval_public_key=_b64encode(pub2.public_bytes_raw()),
        approval_key_id="kid-v2",
    )
    assert (
        verify_approval_grant(
            grant, step_id="s1", fingerprint="fp1", owner_id="alice", agent_name="coding_agent", settings=untrusted_cfg
        )
        is None
    )

    # Key rotation: RedisSaver checkpoint signed with old HMAC deserializes cleanly with rotated secret
    with (
        patch("src.agentic.memory.redis_saver.get_settings", return_value=cfg_old),
        patch("src.agentic.memory.redis_saver.RedisSaver._init_redis"),
    ):
        saver_old = RedisSaver(redis_url=None)
        payload = saver_old._sign_and_serialize({"turn": 1}, use_json=True)
    with (
        patch("src.agentic.memory.redis_saver.get_settings", return_value=cfg_rotated),
        patch("src.agentic.memory.redis_saver.RedisSaver._init_redis"),
    ):
        saver_new = RedisSaver(redis_url=None)
        assert saver_new._verify_and_deserialize(payload) == {"turn": 1}

    # ThreadRegistry preserves candidate scoped thread IDs across rotation
    reg_old = ThreadRegistry()
    reg_old.settings = cfg_old
    reg_new = ThreadRegistry()
    reg_new.settings = cfg_rotated
    assert reg_old.scoped_thread_id("pub-1", "alice") in reg_new.candidate_scoped_thread_ids("pub-1", "alice")

    # Restore drill smoke check
    drill_res = run_sqlite_restore_smoke()
    assert drill_res["checksum_verified"] is True and drill_res["audit_valid"] is True


# ---------------------------------------------------------------------------
# P2-4: Conversational fast-path bypasses redundant Reflector & synthesis Finalize
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p2_4_conversational_fast_path_skips_reflector_and_synthesis_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAST_HEURISTIC_PLANNER", "true")
    graph = create_supervisor_graph(checkpointer=MemorySaver())

    class _FastA2A:
        async def call_agent_stream(self, **kwargs: Any):  # type: ignore[no-untyped-def]
            yield {"event": "completed", "output": "Hello alice@example.com from ChatAgent!"}

    with (
        patch("src.agentic.orchestrator.nodes.dispatch.get_a2a_client", return_value=_FastA2A()),
        patch("src.agentic.orchestrator.nodes.reflector.get_llm", side_effect=AssertionError("Reflector LLM must not be called")),
        patch("src.agentic.orchestrator.nodes.finalize.get_llm", side_effect=AssertionError("Finalize LLM must not be called")),
    ):
        res = await graph.ainvoke(
            build_turn_input(
                user_query="Xin chào hệ thống!",
                thread_id="thr-fast-path",
                metadata={"user_id": "alice", "tenant_id": "org-a"},
            ),
            config={"configurable": {"thread_id": "thr-fast-path"}},
        )
    assert "[REDACTED_EMAIL]" in res["final_answer"]
    assert "from ChatAgent!" in res["final_answer"]


# ---------------------------------------------------------------------------
# P1-10, P1-13, P1-14, P1-15, P2-7, P2-8, P2-9: Manifests, serving scripts & docs
# ---------------------------------------------------------------------------
def test_p1_p2_manifests_serving_and_docs_consistency() -> None:
    chat_sh = (ROOT / "deploy" / "serving" / "llamacpp-chat.sh").read_text(encoding="utf-8")
    embed_sh = (ROOT / "deploy" / "serving" / "llamacpp-embed.sh").read_text(encoding="utf-8")
    assert "--embedding false" not in chat_sh
    assert "--jinja" in chat_sh
    assert "--embedding true" not in embed_sh
    assert "--embedding" in embed_sh

    netpols = {
        d["metadata"]["name"]: d
        for d in yaml.safe_load_all((ROOT / "deploy" / "k8s" / "network-policy.yaml").read_text(encoding="utf-8"))
        if isinstance(d, dict) and d.get("kind") == "NetworkPolicy"
    }
    api_ports = {p["port"] for rule in netpols["allow-agentic-api"]["spec"]["egress"] for p in rule.get("ports", [])}
    assert {7233, 443} <= api_ports

    for pvc_file in ("rag-storage.yaml", "mcp-scratch-storage.yaml"):
        pvc = yaml.safe_load((ROOT / "deploy" / "k8s" / pvc_file).read_text(encoding="utf-8"))
        assert pvc["spec"].get("storageClassName")

    kust = yaml.safe_load((ROOT / "deploy" / "k8s" / "kustomization.yaml").read_text(encoding="utf-8"))
    assert {"serving.yaml", "datastores.yaml"} <= set(kust["resources"])

    mig = yaml.safe_load((ROOT / "deploy" / "k8s" / "db-migration-job.yaml").read_text(encoding="utf-8"))
    annotations = mig["metadata"].get("annotations", {})
    assert "argocd.argoproj.io/hook" not in annotations
    assert annotations.get("argocd.argoproj.io/sync-wave") == "1"

    ing = yaml.safe_load((ROOT / "deploy" / "k8s" / "ingress.yaml").read_text(encoding="utf-8"))
    assert ing["metadata"]["annotations"]["nginx.ingress.kubernetes.io/enable-cors"] == "false"

    assert (ROOT / "deploy" / "overlays" / "production" / "kustomization.yaml").exists()
    assert (ROOT / "docs" / "slos.md").exists()

    rollback = (ROOT / "docs" / "runbooks" / "rollback.md").read_text(encoding="utf-8")
    assert "langgraph:checkpoint:*" not in rollback
    assert "agentic:checkpoint:*" in rollback

    arch = (ROOT / "Architecture.md").read_text(encoding="utf-8")
    assert "llama-chat-backup" in arch
