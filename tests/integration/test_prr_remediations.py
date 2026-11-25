"""Comprehensive regression, contract, and E2E tests for PRR remediations."""

from __future__ import annotations

import asyncio
import base64
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import jwt
import pytest
import uvicorn
import yaml
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.errors import GraphInterrupt
from llama_index.core.schema import NodeWithScore, TextNode
from mcp.shared.exceptions import MCPError

from src.agentic.a2a.cards import AgentCard
from src.agentic.a2a.client import A2AClient, A2ACommunicationError
from src.agentic.a2a.executor import A2AAgentExecutor, A2ATaskPayload, A2ATaskRequest
from src.agentic.api.deps import get_current_user, get_graph
from src.agentic.api.main import app
from src.agentic.core.config import Settings, get_settings
from src.agentic.observability.metrics import HITL_PENDING_APPROVALS
from src.agentic.orchestrator.nodes.dispatch import dispatch_node
from src.agentic.orchestrator.nodes.hitl import hitl_node
from src.agentic.orchestrator.nodes.planner import _apply_risk_policy
from src.agentic.orchestrator.state import AgentState, Plan, Step
from src.agentic.rag.retriever import HybridRetriever
from src.agentic.rag.tools import search_policy
from src.agentic.safety.guardrails import GuardrailManager
from src.agentic.safety.policy import PolicyManager
from src.agentic.tools.mcp_client import MCPClientManager
from src.agentic.tools.servers import repo_server
from src.agentic.tools.servers.code_exec_server import _truncate_output
from src.agentic.tools.servers.security import secure_mcp_app

ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# P0-1: JWT -> state.metadata -> A2A -> RAG tenant_id E2E isolation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p0_1_jwt_tenant_propagates_end_to_end_to_rag_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real JWT tenant_id flows through Chat API -> dispatch -> A2A -> search_policy."""
    settings = get_settings()
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": "alice",
            "role": "user",
            "roles": ["user"],
            "department": "engineering",
            "client_id": "acme-web",
            "tenant_id": "org-acme",
            "aud": settings.jwt_audience,
            "iss": settings.jwt_issuer,
            "iat": now,
            "exp": now + 3600,
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )

    user = await get_current_user(authorization=f"Bearer {token}")
    assert user["tenant_id"] == "org-acme"
    assert user["user_id"] == "alice"

    # Build a fake HybridRetriever configured with production-strict _is_permitted
    retriever = HybridRetriever.__new__(HybridRetriever)
    retriever.settings = SimpleNamespace(require_embedding_service=True)

    acme_node = NodeWithScore(
        node=TextNode(
            text="Acme secret policy: 2FA required.",
            metadata={
                "doc_id": "d-acme",
                "file_name": "acme_policy.md",
                "tenant_id": "org-acme",
                "department": "engineering",
                "allowed_roles": ["user"],
                "acl": ["org-acme", "engineering", "role:user"],
            },
        ),
        score=0.95,
    )
    other_node = NodeWithScore(
        node=TextNode(
            text="Other org secret policy.",
            metadata={
                "doc_id": "d-other",
                "file_name": "other_policy.md",
                "tenant_id": "org-other",
                "department": "engineering",
                "allowed_roles": ["user"],
                "acl": ["org-other", "engineering", "role:user"],
            },
        ),
        score=0.99,
    )
    public_node = NodeWithScore(
        node=TextNode(
            text="Default public policy.",
            metadata={
                "doc_id": "d-public",
                "file_name": "public.md",
                "tenant_id": "public",
                "department": "*",
                "allowed_roles": ["*"],
                "acl": ["public"],
            },
        ),
        score=0.90,
    )

    def fake_retrieve(query: str, top_k: int = 6, filters: dict | None = None) -> list[NodeWithScore]:
        return [
            item
            for item in (other_node, public_node, acme_node)
            if retriever._is_permitted(item, filters or {})
        ]

    retriever.retrieve = fake_retrieve  # type: ignore[method-assign]

    async def fake_rag_ainvoke(state: dict) -> dict:
        out = search_policy("2FA policy")
        return {"messages": [AIMessage(content=out)]}

    fake_rag_graph = SimpleNamespace(ainvoke=AsyncMock(side_effect=fake_rag_ainvoke))
    executor = A2AAgentExecutor(
        card=AgentCard(name="rag_agent", description="RAG", url="http://localhost:9101", skills=[]),
        graph=fake_rag_graph,
    )

    a2a_client = A2AClient()

    async def fake_stream(**kwargs):
        req = A2ATaskRequest(
            task_id=kwargs["step_id"],
            caller="supervisor",
            target_agent=kwargs["target_agent"],
            payload=A2ATaskPayload(
                instruction=kwargs["instruction"], context=kwargs.get("context") or {}
            ),
            metadata=kwargs.get("metadata") or {},
        )
        async for ev in executor.execute_stream(req):
            yield ev

    a2a_client.call_agent_stream = fake_stream  # type: ignore[method-assign]

    fake_cache = MagicMock()
    fake_cache.get_or_compute.side_effect = lambda q, fn, **kw: fn()
    fake_reranker = MagicMock()
    fake_reranker.mode = "heuristic"
    fake_reranker.rerank.side_effect = lambda q, nodes: nodes

    class _Snapshot:
        next: tuple[()] = ()
        tasks: tuple[()] = ()

    class _E2ERagGraph:
        async def ainvoke(self, initial_input: dict, config: object) -> dict:
            step = Step(
                id="step_1",
                description="Search policy",
                agent="rag",
                input_query="2FA policy",
                risk_level="low",
            )
            state = AgentState(
                messages=initial_input["messages"],
                thread_id=initial_input["thread_id"],
                metadata=initial_input["metadata"],
                plan=Plan(goal="2FA policy", steps=[step]),
            )
            res = await dispatch_node(state)
            updated_plan = res["plan"]
            return {
                "plan": updated_plan,
                "final_answer": updated_plan.steps[0].output,
            }

        async def aget_state(self, config: object) -> _Snapshot:
            return _Snapshot()

    monkeypatch.setattr("src.agentic.orchestrator.nodes.dispatch.get_a2a_client", lambda: a2a_client)
    monkeypatch.setattr("src.agentic.rag.tools.get_hybrid_retriever", lambda: retriever)
    monkeypatch.setattr("src.agentic.rag.tools.get_rag_cache", lambda: fake_cache)
    monkeypatch.setattr(
        "src.agentic.rag.tools.CrossEncoderReranker",
        lambda top_n=3, **_kwargs: fake_reranker,
    )

    app.dependency_overrides[get_graph] = lambda: _E2ERagGraph()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/chat",
                headers={"Authorization": f"Bearer {token}"},
                json={"query": "What is the 2FA policy?", "thread_id": "e2e-tenant-1"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "Acme secret policy: 2FA required." in body["answer"]
        assert "Other org secret policy" not in body["answer"]
        assert "Default public policy" not in body["answer"]
    finally:
        app.dependency_overrides.pop(get_graph, None)


# ---------------------------------------------------------------------------
# P0-2: Real stateless Streamable-HTTP MCP server contract test (no mock)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p0_2_real_stateless_streamable_http_mcp_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise MCPClientManager._remote_call against a real FastMCP streamable-HTTP server."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    monkeypatch.setenv("REQUIRE_AUTHENTICATION", "true")
    monkeypatch.setenv("S2S_INTERNAL_TOKEN", "contract-s2s-token")
    get_settings.cache_clear()

    try:
        mcp_app = secure_mcp_app(
            repo_server.mcp.streamable_http_app(stateless_http=True, json_response=True)
        )
        config = uvicorn.Config(mcp_app, host="127.0.0.1", port=port, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started, "Real MCP streamable-HTTP server failed to start"

        mgr = MCPClientManager()
        mgr.settings = Settings(
            environment="test",
            require_authentication=True,
            s2s_internal_token="contract-s2s-token",
            mcp_repo_url=f"http://127.0.0.1:{port}/mcp",
            mcp_transport="streamable-http",
            use_remote_mcp=True,
        )

        # Two sequential calls without sticky session prove statelessness works.
        listing = await mgr._remote_call("repo_list_files", {"directory": "configs", "max_depth": 1})
        assert "policies.yaml" in listing

        # RBAC bypass regression: data/index and data/raw are denied via repo_read_file.
        denied_index = await mgr._remote_call(
            "repo_read_file", {"file_path": "data/raw/security_policy.md"}
        )
        assert "Security Violation" in denied_index

        # Missing approval grant on repo_write_file must be rejected by real server middleware (403).
        with pytest.raises(ExceptionGroup) as exc_info:
            await mgr._remote_call(
                "repo_write_file",
                {"file_path": "data/scratch/unapproved.txt", "content": "nope"},
            )
        pending: list[BaseException] = [exc_info.value]
        leaves: list[BaseException] = []
        while pending:
            current = pending.pop()
            if isinstance(current, BaseExceptionGroup):
                pending.extend(current.exceptions)
            else:
                leaves.append(current)
        assert any(isinstance(exc, MCPError) for exc in leaves)
    finally:
        if "server" in locals():
            server.should_exit = True
            thread.join(timeout=3.0)
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# P0-3 & P1 CI/K8s manifest invariants
# ---------------------------------------------------------------------------
def test_p0_3_and_k8s_hardening_invariants() -> None:
    ci_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "STRICT_MIGRATION: \"true\"" in ci_text
    assert "newName: ghcr.io/agentic-platform/{comp}" in ci_text
    assert "newName: ghcr.io/{repo_lower}/{comp}" in ci_text

    ns_docs = list(yaml.safe_load_all((ROOT / "deploy" / "k8s" / "namespace.yaml").read_text()))
    sandbox_ns = next(d for d in ns_docs if d["metadata"]["name"] == "agentic-sandbox")
    assert sandbox_ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"

    net_docs = list(
        yaml.safe_load_all((ROOT / "deploy" / "k8s" / "sandbox-network-policy.yaml").read_text())
    )
    assert {"NetworkPolicy", "ResourceQuota", "LimitRange"}.issubset(
        {d["kind"] for d in net_docs}
    )

    for job_file in ("db-migration-job.yaml", "checkpoint-retention.yaml", "backup.yaml"):
        docs = list(yaml.safe_load_all((ROOT / "deploy" / "k8s" / job_file).read_text()))
        text = yaml.safe_dump(docs)
        assert "sidecar.istio.io/inject: 'true'" in text or 'sidecar.istio.io/inject: "true"' in text
        assert "holdApplicationUntilProxyStarts" in text
        assert "terminationDrainDuration" in text

    dep_docs = list(yaml.safe_load_all((ROOT / "deploy" / "k8s" / "deployment.yaml").read_text()))
    mcp_svcs = [
        d for d in dep_docs
        if d and d.get("kind") == "Service" and d["metadata"]["name"].startswith("agentic-mcp-")
    ]
    assert len(mcp_svcs) == 3
    assert all(svc["spec"].get("sessionAffinity") == "ClientIP" for svc in mcp_svcs)

    alerts = yaml.safe_load((ROOT / "deploy" / "monitoring" / "alerts.yaml").read_text())
    alert_names = {r["alert"] for g in alerts["groups"] for r in g["rules"]}
    assert {
        "CheckpointPersistErrors",
        "RateLimitUnavailable",
        "RAGRetrievalDegraded",
    }.issubset(alert_names)


# ---------------------------------------------------------------------------
# P1: A2A idempotency, HITL gauge/four-eyes/tenant, planner risk, guardrails
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p1_a2a_idempotency_and_no_midstream_retry() -> None:
    calls = 0

    class _OnceGraph:
        async def ainvoke(self, state: dict) -> dict:
            nonlocal calls
            calls += 1
            return {"messages": [AIMessage(content=f"ran-{calls}")]}

    executor = A2AAgentExecutor(
        card=AgentCard(name="coding_agent", description="Coding", url="http://localhost:9102", skills=[]),
        graph=_OnceGraph(),
    )
    req = A2ATaskRequest(
        task_id="step_1",
        target_agent="coding_agent",
        payload=A2ATaskPayload(instruction="run once"),
        idempotency_key="fp-123",
    )
    r1 = await executor.execute(req)
    r2 = await executor.execute(req)
    assert calls == 1
    assert r1.output == "ran-1" and r2.output == "ran-1"

    # Mid-stream TransportError after first frame must NOT re-POST.
    client = A2AClient()
    client.settings = Settings(
        environment="test", a2a_max_retries=3, allow_local_agent_fallback=False
    )
    post_count = 0

    class _BrokenMidStream:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def aiter_lines(self):
            yield 'data: {"event": "token", "token": "hi"}'
            yield ""
            raise httpx.ReadTimeout("stream dropped mid-flight")

    fake_http = MagicMock()

    def _stream(*args, **kwargs):
        nonlocal post_count
        post_count += 1
        return _BrokenMidStream()

    fake_http.stream = _stream
    client._get_http_client = lambda timeout=60.0: fake_http  # type: ignore[method-assign]

    with pytest.raises(A2ACommunicationError, match="mid-stream"):
        async for _ in client.call_agent_stream("chat", "s1", "hello"):
            pass
    assert post_count == 1


@pytest.mark.asyncio
async def test_p1_hitl_gauge_stays_elevated_on_graph_interrupt_and_enforces_four_eyes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    step = Step(
        id="step_1",
        description="Run shell",
        agent="coding",
        input_query="echo hi",
        risk_level="high",
    )
    state = AgentState(
        messages=[HumanMessage(content="echo hi")],
        plan=Plan(goal="echo hi", steps=[step]),
        thread_id="thr-gauge-1",
        metadata={"user_id": "alice", "tenant_id": "org-a"},
    )

    def _pause(_payload: object) -> None:
        raise GraphInterrupt(())

    monkeypatch.setattr("src.agentic.orchestrator.nodes.hitl.interrupt", _pause)
    before = HITL_PENDING_APPROVALS._value.get()
    try:
        with pytest.raises(GraphInterrupt):
            await hitl_node(state)
        assert HITL_PENDING_APPROVALS._value.get() == before + 1
    finally:
        HITL_PENDING_APPROVALS.dec()

    # Cross-tenant & four-eyes check in /approvals/{thread_id}/resume
    from fastapi import HTTPException

    from src.agentic.api.routes.approvals import resume_hitl_endpoint
    from src.agentic.core.schemas import ApprovalRequest, Decision

    fake_graph = MagicMock()
    fake_graph.aget_state = AsyncMock(
        return_value=SimpleNamespace(
            next=("hitl",),
            tasks=(SimpleNamespace(interrupts=(SimpleNamespace(value={"step_id": "step_1"}),)),),
            values={"metadata": {"tenant_id": "org-a"}},
        )
    )
    fake_registry = MagicMock()
    fake_registry.settings = SimpleNamespace(environment="production")
    fake_registry.owner = AsyncMock(return_value="alice")
    fake_registry.scoped_thread_id = lambda tid, oid, tenant="default": f"scoped:{tenant}:{oid}:{tid}"
    monkeypatch.setattr("src.agentic.api.routes.approvals.get_thread_registry", lambda: fake_registry)

    req = ApprovalRequest(thread_id="t1", step_id="step_1", decision=Decision(approved=True))

    with pytest.raises(HTTPException) as cross_exc:
        await resume_hitl_endpoint(
            "t1", req, graph=fake_graph, current_user={"user_id": "bob", "tenant_id": "org-b"}
        )
    assert cross_exc.value.status_code == 403

    b64_key = base64.urlsafe_b64encode(b"p" * 32).decode("ascii").rstrip("=")
    prod_settings = Settings(
        environment="production",
        require_authentication=True,
        jwt_secret_key="prod-jwt-secret-key-for-unit-tests-only",
        jwt_algorithm="RS256",
        jwt_allowed_algorithms=["RS256"],
        jwt_jwks_url="https://auth.example.com/.well-known/jwks.json",
        s2s_internal_token="prod-s2s-internal-token-for-unit-tests",
        checkpoint_hmac_secret="prod-checkpoint-hmac-secret-32chars",
        use_postgres_checkpoint=True,
        postgres_uri="postgresql://agentic:secret@pgbouncer:5432/agentic_db",
        allowed_origins=["https://app.example.com"],
        approval_private_key=b64_key,
        qdrant_api_key="prod-qdrant-api-key",
        neo4j_password="prod-neo4j-test-secret",
        litellm_api_key="sk-prod-test-key",
    )
    monkeypatch.setattr("src.agentic.api.routes.approvals.get_settings", lambda: prod_settings)
    with pytest.raises(HTTPException) as self_exc:
        await resume_hitl_endpoint(
            "t1", req, graph=fake_graph, current_user={"user_id": "alice", "tenant_id": "org-a"}
        )
    assert self_exc.value.status_code == 403


def test_p1_risk_by_tool_guardrails_truncation_and_policy_resilience(tmp_path: Path) -> None:
    # 1. Read-only coding step remains low risk; mutating coding step is high risk.
    plan = Plan(
        goal="Inspect files",
        steps=[
            Step(
                id="s1",
                description="List files in deploy/docker",
                agent="coding",
                input_query="Liệt kê danh sách tất cả các file trong thư mục deploy/docker.",
                risk_level="low",
            ),
            Step(
                id="s2",
                description="Write config file",
                agent="coding",
                input_query="Ghi đè tệp cấu hình configs/test.yaml",
                risk_level="low",
            ),
        ],
    )
    _apply_risk_policy(plan)
    assert plan.steps[0].risk_level == "low"
    assert plan.steps[1].risk_level == "high"

    # 2. Guardrail false-positive reductions
    gm = GuardrailManager()
    assert "graceful shutdown" in gm.sanitize_input("How does graceful shutdown work?")
    assert gm.mask_pii("Order ID 987654321 confirmed") == "Order ID 987654321 confirmed"
    assert "[REDACTED_NATIONAL_ID]" in gm.mask_pii("CMND 123456789")
    assert gm.mask_pii("Ref 1234567890123456") == "Ref 1234567890123456"
    assert "hate speech policy" in gm.sanitize_output("See our hate speech policy.")

    # 3. Sandbox truncation
    big = "A" * 50_000
    truncated = _truncate_output(big)
    assert len(truncated) < 33_500
    assert "output truncated" in truncated

    # 4. Policy default-deny, timeout, and resilient hot-reload
    policy_file = tmp_path / "policies.yaml"
    policy_file.write_text(
        "tools:\n  repo_read_file:\n    risk_level: low\n    require_approval: false\n    timeout_seconds: 10\n",
        encoding="utf-8",
    )
    pm = PolicyManager(policy_path=policy_file)
    assert pm.get_tool_timeout("repo_read_file") == 10
    assert pm.get_tool_risk("brand_new_tool") == "high"
    assert pm.is_approval_required("brand_new_tool") is True

    policy_file.write_text("tools: [invalid: {", encoding="utf-8")
    reloaded = pm.reload()
    assert "repo_read_file" in reloaded["tools"]


# ---------------------------------------------------------------------------
# P1/P2 Remaining Fixes: Tool-Call HITL, JWKS/Revocation, Degraded Rate Limit,
# Single-File CDC, LTM Retention, LangSmith Redaction, Web Search API, K8s
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_p1_two_phase_tool_call_hitl_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    """When require_tool_call_approval=True, code_exec_bash needs exact arg approval."""
    from src.agentic.core.errors import ToolCallApprovalRequired
    from src.agentic.safety.approvals import canonical_args_digest

    b64_priv = base64.urlsafe_b64encode(b"k" * 32).decode("ascii").rstrip("=")
    test_settings = Settings(
        environment="test",
        require_tool_call_approval=True,
        use_remote_mcp=False,
        approval_private_key=b64_priv,
    )
    mgr = MCPClientManager()
    mgr.settings = test_settings
    mgr._local["code_exec_bash"] = AsyncMock(return_value="bash-ok")

    step = Step(
        id="step_1",
        description="Run bash command",
        agent="coding",
        input_query="echo hello",
        risk_level="high",
    )

    class _BashAgentGraph:
        async def ainvoke(self, state: dict) -> dict:
            out = await mgr._invoke("coding_agent", "code_exec_bash", command="echo hello")
            return {"messages": [AIMessage(content=out)]}

    executor = A2AAgentExecutor(
        card=AgentCard(name="coding_agent", description="Coding", url="http://localhost:9102", skills=[]),
        graph=_BashAgentGraph(),
    )
    a2a_client = A2AClient()

    async def fake_stream(**kwargs):
        req = A2ATaskRequest(
            task_id=kwargs["step_id"],
            caller="supervisor",
            target_agent=kwargs["target_agent"],
            payload=A2ATaskPayload(
                instruction=kwargs["instruction"], context=kwargs.get("context") or {}
            ),
            metadata=kwargs.get("metadata") or {},
        )
        async for ev in executor.execute_stream(req):
            yield ev

    a2a_client.call_agent_stream = fake_stream  # type: ignore[method-assign]
    monkeypatch.setattr("src.agentic.orchestrator.nodes.dispatch.get_a2a_client", lambda: a2a_client)
    monkeypatch.setattr("src.agentic.orchestrator.nodes.dispatch.get_settings", lambda: test_settings)

    from src.agentic.orchestrator.nodes.hitl import compute_step_fingerprint

    fp = compute_step_fingerprint(step)
    state = AgentState(
        messages=[HumanMessage(content="echo hello")],
        plan=Plan(goal="Run bash", steps=[step]),
        approved_step_ids=["step_1"],
        approved_step_fingerprints={"step_1": fp},
        metadata={"user_id": "alice", "tenant_id": "org-a"},
    )

    # Phase 1: step-approved only -> sub-agent hits ToolCallApprovalRequired -> dispatch pauses for tool args
    res1 = await dispatch_node(state)
    assert res1["pending_tool_approval"] is not None
    assert res1["pending_tool_approval"]["tool_name"] == "code_exec_bash"
    assert res1["pending_tool_approval"]["arguments"] == {"command": "echo hello"}
    assert res1["plan"].steps[0].status == "pending"

    # Phase 2: hitl_node surfaces exact tool_name + tool_arguments and records approved_tool_call_digests
    captured_interrupts: list[dict] = []

    def _approve_tool(payload: dict) -> dict:
        captured_interrupts.append(payload)
        return {"approved": True, "approver_id": "bob"}

    monkeypatch.setattr("src.agentic.orchestrator.nodes.hitl.interrupt", _approve_tool)
    monkeypatch.setattr("src.agentic.orchestrator.nodes.hitl.record_audit_event", lambda **kw: "ok")
    state_waiting = state.model_copy(
        update={
            "plan": res1["plan"],
            "pending_tool_approval": res1["pending_tool_approval"],
        }
    )
    res_hitl = await hitl_node(state_waiting)
    assert captured_interrupts[0]["tool_name"] == "code_exec_bash"
    assert captured_interrupts[0]["tool_arguments"] == {"command": "echo hello"}
    expected_digest = canonical_args_digest("code_exec_bash", {"command": "echo hello"})
    assert expected_digest in res_hitl["approved_tool_call_digests"]["step_1"]

    # Phase 3: dispatch with approved_tool_call_digests succeeds
    state_approved = state_waiting.model_copy(
        update={
            "pending_tool_approval": None,
            "approved_tool_call_digests": res_hitl["approved_tool_call_digests"],
        }
    )
    res2 = await dispatch_node(state_approved)
    assert res2["plan"].steps[0].status == "completed"
    assert res2["plan"].steps[0].output == "bash-ok"
    assert ToolCallApprovalRequired.__name__ == "ToolCallApprovalRequired"


@pytest.mark.asyncio
async def test_p1_p2_jwks_revocation_degraded_limiter_cdc_ltm_langsmith_web_k8s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.agentic.api.deps import decode_bearer_jwt
    from src.agentic.api.middleware import FailClosedRateLimiter
    from src.agentic.memory.long_term import LongTermStore
    from src.agentic.observability.langsmith_tracer import redact_trace_payload
    from src.agentic.rag.ingestion import ingest_single_document
    from src.agentic.tools.servers.web_search_server import search_web_core

    # 1. JWT JTI revocation check
    s = Settings(environment="test", jwt_secret_key="x" * 40, jwt_revocation_check=True,
                 require_authentication=True, s2s_internal_token="t" * 32,
                 checkpoint_hmac_secret="c" * 32,
                 postgres_uri="postgresql://u:p@db:5432/app", use_postgres_checkpoint=True,
                 allowed_origins=["https://app.example.com"],
                 approval_private_key=base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("="),
                 qdrant_api_key="q", neo4j_password="n", litellm_api_key="sk-real")
    now = int(time.time())
    tok = jwt.encode(
        {"sub": "u1", "jti": "revoked-jti", "aud": s.jwt_audience, "iss": s.jwt_issuer, "iat": now, "exp": now + 60},
        s.jwt_secret_key,
        algorithm="HS256",
    )
    monkeypatch.setattr("src.agentic.api.deps._is_jti_revoked", lambda jti, _cfg: jti == "revoked-jti")
    with pytest.raises(jwt.InvalidTokenError, match="revoked"):
        decode_bearer_jwt(tok, s)

    # 2. Degraded local fallback on FailClosedRateLimiter
    degraded_settings = Settings(
        environment="test",
        rate_limit_per_minute=20,
        rate_limit_degraded_local_fallback=True,
        enable_redis_rate_limit=False,
    )
    limiter = FailClosedRateLimiter(settings=degraded_settings)
    limiter.try_recover = lambda: None  # type: ignore[method-assign]
    allowed, retry = limiter.is_allowed("tenant:a:u1")
    assert allowed is True and retry == 0

    # 3. Single-file CDC ingestion + LTM retention
    doc_path = tmp_path / "delta.md"
    doc_path.write_text("# Delta\nSingle document incremental indexing test.", encoding="utf-8")
    idx_dir = tmp_path / "idx"
    from llama_index.core import Settings as LlamaSettings
    from llama_index.core.embeddings import MockEmbedding

    monkeypatch.setattr(LlamaSettings, "_embed_model", MockEmbedding(embed_dim=1024))
    with (
        patch("src.agentic.rag.embedding.configure_embedding_model"),
        patch("src.agentic.rag.retriever.get_qdrant_vector_store", return_value=None),
    ):
        count = ingest_single_document(doc_path, index_persist_dir=idx_dir, tenant_id="org-delta")
        assert count >= 1

    ltm = LongTermStore(storage_dir=tmp_path / "ltm", force_sqlite=True)
    ltm.add_episodic("t1", "Old memory entry", metadata={"user_id": "alice"})
    assert ltm.delete_older_than(max_age_days=90) == 0

    # 4. LangSmith payload redaction
    scrubbed = redact_trace_payload(
        {"query": "Contact alice@acme.com with sk-1234567890123456789012", "api_key": "secret-val"}
    )
    assert "[REDACTED_EMAIL]" in scrubbed["query"]
    assert "[REDACTED_SECRET]" in scrubbed["query"]
    assert scrubbed["api_key"] == "[REDACTED_SECRET]"

    # 5. Structured Web Search API support
    fake_resp = SimpleNamespace(
        status_code=200,
        json=lambda: {"results": [{"title": "Docs", "snippet": "Stateless MCP guide"}]},
    )

    class _FakeAsyncClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            return fake_resp

    monkeypatch.setattr(
        "src.agentic.core.config.get_settings",
        lambda: Settings(environment="test", web_search_api_url="https://search.internal/api"),
    )
    monkeypatch.setattr("src.agentic.tools.servers.web_search_server.httpx.AsyncClient", _FakeAsyncClient)
    ws_out = await search_web_core("stateless mcp")
    assert "Stateless MCP guide" in ws_out

    # 6. Remaining K8s hardening invariants (admin ingress exclusion, DB role split, bootstrap, Qdrant backup)
    ingress_doc = yaml.safe_load((ROOT / "deploy" / "k8s" / "ingress.yaml").read_text())
    ingress_paths = [p["path"] for p in ingress_doc["spec"]["rules"][0]["http"]["paths"]]
    assert "/api" not in ingress_paths
    assert "/api/v1/admin" not in ingress_paths
    assert "/api/v1/chat" in ingress_paths

    ext_docs = list(yaml.safe_load_all((ROOT / "deploy" / "k8s" / "external-secret.yaml").read_text()))
    api_sec = next(d for d in ext_docs if d.get("metadata", {}).get("name") == "agentic-api-secrets")
    maint_sec = next(
        d for d in ext_docs if d.get("metadata", {}).get("name") == "agentic-maintenance-secrets"
    )
    assert any(i["remoteRef"]["property"] == "postgres_app_uri" for i in api_sec["spec"]["data"])
    assert any(
        i["remoteRef"]["property"] == "postgres_migration_uri" for i in maint_sec["spec"]["data"]
    )
    assert (ROOT / "deploy" / "k8s" / "rag-bootstrap-job.yaml").exists()
    backup_docs = list(yaml.safe_load_all((ROOT / "deploy" / "k8s" / "backup.yaml").read_text()))
    assert any(d.get("metadata", {}).get("name") == "agentic-qdrant-backup" for d in backup_docs)
