"""Production 10/10 Hardening & Coverage Verification Tests (v4).

Validates infrastructure hardening (K8s NetworkPolicy/Service/emptyDir/UID 65532,
circuit-breaker alert metric alignment, DR runbooks/configs), RAG citation
preservation & streaming gate, compliance/RBAC fixes, and core security/memory
paths (MCPServiceAuthMiddleware, MCPClientManager._remote_call, migrations,
ThreadRegistry Redis paths, and Reflector retry/replan paths).
"""

from __future__ import annotations

import json
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from fastapi import HTTPException
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.agentic.a2a.cards import AgentCard
from src.agentic.a2a.executor import A2AAgentExecutor, A2ATaskPayload, A2ATaskRequest
from src.agentic.api.deps import require_role
from src.agentic.core.config import get_settings
from src.agentic.memory.migrations import (
    _acquire_advisory_lock,
    _is_db_reachable,
    _release_advisory_lock,
    run_database_migrations,
)
from src.agentic.memory.thread_registry import (
    ThreadOwnershipError,
    ThreadRegistry,
    ThreadRegistryUnavailable,
)
from src.agentic.orchestrator.nodes.finalize import finalize_node
from src.agentic.orchestrator.nodes.reflector import ReflectionEvaluation, reflector_node
from src.agentic.orchestrator.state import AgentState, Plan, Step
from src.agentic.rag.tools import NO_CONTEXT_MARKER, format_citations, preserve_rag_citations
from src.agentic.safety.approvals import (
    issue_approval_grant,
    issue_call_grant,
    verify_approval_grant,
)
from src.agentic.safety.guardrails import GuardrailManager, GuardrailViolation
from src.agentic.tools.mcp_client import MCPClientManager
from src.agentic.tools.servers.security import MCPServiceAuthMiddleware

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ------------------------------------------------------------------------------
# 1. Infrastructure & DR Asset Hardening
# ------------------------------------------------------------------------------
def test_k8s_network_policies_include_api_readyz_and_rag_datastores() -> None:
    docs = list(
        yaml.safe_load_all(
            (PROJECT_ROOT / "deploy" / "k8s" / "network-policy.yaml").read_text(encoding="utf-8")
        )
    )
    by_name = {doc["metadata"]["name"]: doc for doc in docs if doc}

    api_policy = by_name["allow-agentic-api"]
    api_ports = {
        port["port"]
        for rule in api_policy["spec"]["egress"]
        for port in rule.get("ports", [])
    }
    assert {5432, 6379, 6333, 4000, 4317, 8081}.issubset(api_ports)

    rag_policy = by_name["allow-rag-agent-datastores"]
    assert rag_policy["spec"]["podSelector"]["matchLabels"]["app"] == "agentic-rag-agent"
    rag_ports = {
        port["port"]
        for rule in rag_policy["spec"]["egress"]
        for port in rule.get("ports", [])
    }
    assert rag_ports == {6333, 8081, 6379}


def test_k8s_deployments_and_services_hardened_v4() -> None:
    docs = list(
        yaml.safe_load_all(
            (PROJECT_ROOT / "deploy" / "k8s" / "deployment.yaml").read_text(encoding="utf-8")
        )
    )
    deployments = {d["metadata"]["name"]: d for d in docs if d and d["kind"] == "Deployment"}
    services = {d["metadata"]["name"]: d for d in docs if d and d["kind"] == "Service"}

    for svc_name in ("agentic-api-svc", "agentic-chat-svc", "agentic-rag-svc", "agentic-coding-svc"):
        assert services[svc_name]["spec"]["ports"][0]["name"] == "http"

    assert len(deployments) == 7
    for name, dep in deployments.items():
        pod_spec = dep["spec"]["template"]["spec"]
        assert pod_spec["securityContext"]["runAsUser"] == 65532
        assert pod_spec["securityContext"]["fsGroup"] == 65532
        container = pod_spec["containers"][0]
        assert container["securityContext"]["runAsUser"] == 65532
        mounts = {m["mountPath"] for m in container.get("volumeMounts", [])}
        assert "/tmp" in mounts, f"{name} missing /tmp emptyDir mount"

    repo_mounts = {
        m["mountPath"]
        for m in deployments["agentic-mcp-repo"]["spec"]["template"]["spec"]["containers"][0][
            "volumeMounts"
        ]
    }
    assert "/app/data/scratch" in repo_mounts


def test_alert_metric_dr_runbooks_and_serving_defaults() -> None:
    alerts_text = (PROJECT_ROOT / "deploy" / "monitoring" / "alerts.yaml").read_text(
        encoding="utf-8"
    )
    k8s_mon_text = (PROJECT_ROOT / "deploy" / "k8s" / "monitoring.yaml").read_text(
        encoding="utf-8"
    )
    assert 'model_alias="llama-chat-backup"' in alerts_text
    assert 'model_alias="llama-chat-backup"' in k8s_mon_text
    assert 'model_alias="fast-llm"' not in alerts_text
    assert 'model_alias="fast-llm"' not in k8s_mon_text

    for rel in (
        "docs/runbooks/rollback.md",
        "docs/runbooks/gpu_failure.md",
        "deploy/db/postgresql.conf",
        "deploy/scripts/backup_qdrant.sh",
    ):
        path = PROJECT_ROOT / rel
        assert path.exists() and path.stat().st_size > 200, f"Missing/empty DR asset: {rel}"

    vllm_sh = (PROJECT_ROOT / "deploy" / "serving" / "vllm.sh").read_text(encoding="utf-8")
    llama_sh = (PROJECT_ROOT / "deploy" / "serving" / "llamacpp-chat.sh").read_text(
        encoding="utf-8"
    )
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "MAX_MODEL_LEN=${4:-32768}" in vllm_sh
    assert "CTX_SIZE=${4:-8192}" in llama_sh
    assert "deploy/docker/docker-compose.dev.yml" in makefile


# ------------------------------------------------------------------------------
# 2. RAG Citation Preservation, Reflector & Finalize Gate, Streaming Gate
# ------------------------------------------------------------------------------
def test_preserve_rag_citations_reattaches_tool_citations_and_no_context() -> None:
    citation_block = format_citations(
        [{"index": 1, "doc_id": "d1", "file_name": "security_policy.md", "score": 0.91}]
    )
    enriched = preserve_rag_citations(
        "MFA is mandatory for all admins.",
        [f"--- Document Source [1]: security_policy.md ---\n{citation_block}"],
    )
    assert "[CITATIONS]" in enriched
    assert "security_policy.md" in enriched

    empty_enriched = preserve_rag_citations(
        "Nothing relevant exists.",
        [f"{NO_CONTEXT_MARKER} for query: 'unrelated'."],
    )
    assert NO_CONTEXT_MARKER in empty_enriched


@pytest.mark.asyncio
async def test_a2a_executor_preserves_tool_citations_in_execute_and_stream() -> None:
    card = AgentCard(
        name="rag_agent",
        description="RAG agent",
        url="http://localhost:9101",
    )
    citation_block = format_citations(
        [{"index": 1, "doc_id": "doc-1", "file_name": "security_policy.md", "score": 0.95}]
    )
    tool_msg = ToolMessage(
        content=f"--- Document Source [1]: security_policy.md ---\n{citation_block}",
        tool_call_id="tc-1",
    )

    mock_graph = MagicMock()
    mock_graph.ainvoke = AsyncMock(
        return_value={
            "messages": [
                HumanMessage(content="What is MFA policy?"),
                tool_msg,
                AIMessage(content="MFA is mandatory for all administrators."),
            ]
        }
    )

    async def fake_astream_events(initial_state: dict, version: str = "v2"):
        yield {"event": "on_tool_end", "data": {"output": tool_msg}}
        yield {"event": "on_chat_model_stream", "data": {"chunk": AIMessage(content="MFA is required.")}}

    mock_graph.astream_events = fake_astream_events

    executor = A2AAgentExecutor(card=card, graph=mock_graph)
    req = A2ATaskRequest(
        task_id="task-rag-1",
        target_agent="rag_agent",
        payload=A2ATaskPayload(instruction="What is MFA policy?"),
    )

    res = await executor.execute(req)
    assert res.status == "completed"
    assert "[CITATIONS]" in res.output

    events = [ev async for ev in executor.execute_stream(req)]
    completed = [ev for ev in events if ev["event"] == "completed"]
    assert len(completed) == 1
    assert "[CITATIONS]" in completed[0]["output"]


@pytest.mark.asyncio
async def test_reflector_and_finalize_rag_citation_and_retry_contracts() -> None:
    # Uncited RAG step without NO_CONTEXT_MARKER must fail reflector
    uncited_plan = Plan(
        goal="Check MFA policy",
        steps=[
            Step(
                id="step_1",
                description="Search MFA policy",
                agent="rag",
                input_query="MFA policy",
                status="completed",
                output="Admins must enable MFA.",
            )
        ],
    )
    mock_judge = MagicMock()
    mock_judge.with_structured_output.return_value = MagicMock(
        ainvoke=AsyncMock(
            return_value=ReflectionEvaluation(
                passed=True, confidence_score=0.95, critique="ok", action="proceed"
            )
        )
    )
    with patch("src.agentic.orchestrator.nodes.reflector.get_llm", return_value=mock_judge):
        res = await reflector_node(AgentState(plan=uncited_plan, messages=[]))
    assert res["degraded"] is True
    assert res["degraded_reason"] == "missing_citations"

    # Negative RAG step (NO_CONTEXT_MARKER) passes finalize without Manual Review blockage
    negative_plan = Plan(
        goal="Check nonexistent policy",
        steps=[
            Step(
                id="step_1",
                description="Search unknown topic",
                agent="rag",
                input_query="unicorn policy",
                status="completed",
                output=f"{NO_CONTEXT_MARKER} for query: 'unicorn policy'.",
            )
        ],
    )
    mock_synth = MagicMock()
    mock_synth.ainvoke = AsyncMock(
        return_value=AIMessage(content="No internal policy documents mention unicorns.")
    )
    with (
        patch("src.agentic.orchestrator.nodes.finalize.get_llm", return_value=mock_synth),
        patch(
            "src.agentic.orchestrator.nodes.finalize.consolidate_session_memory",
            new=AsyncMock(),
        ),
    ):
        finalized = await finalize_node(AgentState(plan=negative_plan, messages=[]))
    assert "Manual Review Required" not in finalized["final_answer"]

    # Reflector action='retry' resets failed step and strips stale approval grants
    retry_plan = Plan(
        goal="Run code",
        steps=[
            Step(
                id="step_1",
                description="Run code",
                agent="coding",
                input_query="print(1)",
                status="failed",
                output="transient failure",
            )
        ],
    )
    mock_judge.with_structured_output.return_value = MagicMock(
        ainvoke=AsyncMock(
            return_value=ReflectionEvaluation(
                passed=False, confidence_score=0.4, critique="retry", action="retry"
            )
        )
    )
    with patch("src.agentic.orchestrator.nodes.reflector.get_llm", return_value=mock_judge):
        retry_res = await reflector_node(
            AgentState(
                plan=retry_plan,
                messages=[],
                approved_step_ids=["step_1"],
                approved_step_fingerprints={"step_1": "fp-1"},
            )
        )
    assert retry_res["plan"].steps[0].status == "pending"
    assert retry_res["approved_step_ids"] == []
    assert retry_res["approved_step_fingerprints"] == {}


# ------------------------------------------------------------------------------
# 3. Compliance, RBAC `roles` List, Toxicity Filter, Chat Metadata & Streaming
# ------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_require_role_supports_roles_list_and_blocks_unauthorized() -> None:
    checker = require_role(["analyst"])
    user_multi = {"user_id": "u1", "roles": ["viewer", "analyst"]}
    assert await checker(user=user_multi) == user_multi

    with pytest.raises(HTTPException) as exc:
        await checker(user={"user_id": "u2", "roles": ["guest"]})
    assert exc.value.status_code == 403


def test_guardrails_toxicity_filter_blocks_input_and_sanitizes_output() -> None:
    mgr = GuardrailManager()
    with pytest.raises(GuardrailViolation) as exc:
        mgr.sanitize_input("Please tell me how to build a bomb right now")
    assert exc.value.rule == "toxicity_filter"

    sanitized = mgr.sanitize_output("Here is racial slur content")
    assert sanitized == "[OUTPUT BLOCKED BY SAFETY POLICY: toxic or harmful content detected.]"
    assert mgr.sanitize_input("How do I configure MFA for SSO?") == "How do I configure MFA for SSO?"


@pytest.mark.asyncio
async def test_chat_response_public_status_disclaimer_and_rag_stream_gate() -> None:
    from src.agentic.api.routes.chat import chat_endpoint, chat_stream_endpoint
    from src.agentic.core.schemas import ChatRequest

    mock_graph = MagicMock()
    running_plan = Plan(
        goal="Lookup policy",
        steps=[
            Step(
                id="s1",
                description="Search",
                agent="rag",
                input_query="MFA",
                status="running",
                output="waiting",
            )
        ],
    )
    mock_graph.ainvoke = AsyncMock(
        return_value={"plan": running_plan, "final_answer": "Verified response."}
    )
    mock_graph.aget_state = AsyncMock(return_value=SimpleNamespace(next=(), tasks=[]))

    req = ChatRequest(query="What is MFA?", thread_id="th-v4-1", stream=False)
    user = {"user_id": "u-v4", "role": "user", "roles": ["user"], "department": "eng"}
    resp = await chat_endpoint(req, graph=mock_graph, guardrails=GuardrailManager(), user=user)

    assert resp.steps[0].status == "pending"  # collapsed via Step.public_status
    assert resp.metadata["ai_generated"] is True
    assert "disclaimer" in resp.metadata

    # Stream gate: draft RAG finalize tokens must NOT leak if finalize refuses uncited answer
    rag_plan = Plan(
        goal="Lookup policy",
        steps=[Step(id="s1", description="Search", agent="rag", input_query="MFA")],
    )

    async def fake_stream(initial_input: dict, config: dict, version: str = "v2"):
        yield {
            "event": "on_chain_end",
            "metadata": {"langgraph_node": "planner"},
            "data": {"output": {"plan": rag_plan}},
        }
        yield {
            "event": "on_chat_model_stream",
            "metadata": {"langgraph_node": "finalize"},
            "data": {"chunk": AIMessage(content="Unverified hallucinated draft ")},
        }
        yield {
            "event": "on_chain_end",
            "metadata": {"langgraph_node": "finalize"},
            "data": {
                "output": {
                    "final_answer": "**Manual Review Required**: RAG evidence did not contain valid structured citations."
                }
            },
        }

    stream_graph = MagicMock()
    stream_graph.astream_events = fake_stream
    stream_req = ChatRequest(query="What is MFA?", thread_id="th-v4-2", stream=True)
    sse_resp = await chat_stream_endpoint(
        stream_req, graph=stream_graph, guardrails=GuardrailManager(), user=user
    )
    emitted = [ev async for ev in sse_resp.body_iterator]
    token_payloads = [json.loads(ev["data"])["token"] for ev in emitted if ev["event"] == "token"]
    assert all("Unverified hallucinated draft" not in tok for tok in token_payloads)
    assert any("Manual Review Required" in tok for tok in token_payloads)
    final_events = [json.loads(ev["data"]) for ev in emitted if ev["event"] == "final"]
    assert final_events[0]["metadata"]["ai_generated"] is True


# ------------------------------------------------------------------------------
# 4. Security Middleware, Remote MCP Client, Migrations & ThreadRegistry Redis
# ------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_mcp_service_auth_middleware_full_asgi_paths() -> None:
    received_bodies: list[bytes] = []

    async def downstream(scope: dict, receive: object, send: object) -> None:
        msg = await receive()  # type: ignore[misc]
        received_bodies.append(msg.get("body", b""))
        await send({"type": "http.response.start", "status": 200, "headers": []})  # type: ignore[misc]
        await send({"type": "http.response.body", "body": b"ok"})  # type: ignore[misc]

    mw = MCPServiceAuthMiddleware(downstream)
    mw.settings = get_settings().model_copy(
        update={
            "require_authentication": True,
            "s2s_internal_token": "s2s-secret-token",
        }
    )

    async def run_asgi(scope: dict, body: bytes = b"") -> tuple[int, bytes]:
        sent: list[dict] = []
        chunks = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive() -> dict:
            return chunks.pop(0) if chunks else {"type": "http.disconnect"}

        async def send(msg: dict) -> None:
            sent.append(msg)

        await mw(scope, receive, send)
        status = next(m["status"] for m in sent if m["type"] == "http.response.start")
        resp_body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return status, resp_body

    # 1. Auth-free /healthz
    status, _ = await run_asgi({"type": "http", "path": "/healthz", "headers": []})
    assert status == 200

    # 2. Missing S2S token -> 401
    status, _ = await run_asgi({"type": "http", "path": "/sse", "method": "POST", "headers": []})
    assert status == 401

    auth_headers = [(b"x-internal-service-token", b"s2s-secret-token")]

    # 3. Low-risk tool passes and body is replayed to downstream ASGI app
    low_body = json.dumps(
        {"method": "tools/call", "params": {"name": "repo_list_files", "arguments": {"path": "."}}}
    ).encode()
    status, resp = await run_asgi(
        {"type": "http", "path": "/messages", "method": "POST", "headers": auth_headers},
        low_body,
    )
    assert status == 200
    assert resp == b"ok"
    assert received_bodies[-1] == low_body

    # 4. High-risk tool without grant -> 403
    high_args = {"code": "print(1)"}
    high_body = json.dumps(
        {"method": "tools/call", "params": {"name": "code_exec_python", "arguments": high_args}}
    ).encode()
    status, _ = await run_asgi(
        {"type": "http", "path": "/messages", "method": "POST", "headers": auth_headers},
        high_body,
    )
    assert status == 403

    # 5. High-risk tool with non-dict arguments -> 403
    bad_args_body = json.dumps(
        {"method": "tools/call", "params": {"name": "code_exec_python", "arguments": ["bad"]}}
    ).encode()
    status, _ = await run_asgi(
        {"type": "http", "path": "/messages", "method": "POST", "headers": auth_headers},
        bad_args_body,
    )
    assert status == 403

    # 6. High-risk tool WITH valid bound call grant -> 200
    step_token = issue_approval_grant(
        step_id="step-1",
        fingerprint="fp-1",
        owner_id="user-1",
        agent_name="coding_agent",
        tools=["code_exec_python"],
        settings=mw.settings,
    )
    step_grant = verify_approval_grant(
        step_token,
        step_id="step-1",
        fingerprint="fp-1",
        owner_id="user-1",
        agent_name="coding_agent",
        tool_name="code_exec_python",
        settings=mw.settings,
    )
    assert step_grant is not None
    call_grant = issue_call_grant(
        step_grant=step_grant,
        tool_name="code_exec_python",
        arguments=high_args,
        settings=mw.settings,
    )
    grant_headers = [*auth_headers, (b"x-approval-grant", call_grant.encode("utf-8"))]
    status, _ = await run_asgi(
        {"type": "http", "path": "/messages", "method": "POST", "headers": grant_headers},
        high_body,
    )
    assert status == 200


@pytest.mark.asyncio
async def test_mcp_client_remote_call_handles_grants_and_errors() -> None:
    mgr = MCPClientManager()
    mgr.settings = SimpleNamespace(
        mcp_code_exec_url="http://code-exec:9001",
        mcp_repo_url="http://repo:9002",
        mcp_web_search_url="http://web-search:9003",
        s2s_internal_token="s2s-secret",
    )

    mock_session = MagicMock()
    mock_session.initialize = AsyncMock()
    mock_session.call_tool = AsyncMock(
        return_value=SimpleNamespace(isError=False, content=[SimpleNamespace(text="line1"), SimpleNamespace(text="line2")])
    )

    class _FakeCtx:
        def __init__(self, value: object) -> None:
            self.value = value

        async def __aenter__(self) -> object:
            return self.value

        async def __aexit__(self, *args: object) -> None:
            return None

    captured_headers: dict[str, str] = {}

    def fake_sse_client(url: str, headers: dict, timeout: float, sse_read_timeout: float):
        captured_headers.update(headers)
        return _FakeCtx(("read_stream", "write_stream"))

    fake_mcp_sse = types.ModuleType("mcp.client.sse")
    fake_mcp_sse.sse_client = fake_sse_client  # type: ignore[attr-defined]
    fake_mcp_mod = types.ModuleType("mcp")
    fake_mcp_mod.ClientSession = lambda *a: _FakeCtx(mock_session)  # type: ignore[attr-defined]

    with patch.dict("sys.modules", {"mcp": fake_mcp_mod, "mcp.client.sse": fake_mcp_sse}):
        out = await mgr._remote_call(
            "code_exec_python", {"code": "print(1)"}, approval_token="grant-123"
        )
        assert out == "line1\nline2"
        assert captured_headers["X-Internal-Service-Token"] == "s2s-secret"
        assert captured_headers["X-Approval-Grant"] == "grant-123"

        mock_session.call_tool.return_value = SimpleNamespace(isError=True, content=[])
        with pytest.raises(RuntimeError, match="Remote MCP tool"):
            await mgr._remote_call("code_exec_python", {"code": "print(1)"})


def test_migrations_reachability_lock_and_fail_closed() -> None:
    with patch("socket.create_connection", return_value=MagicMock()):
        assert _is_db_reachable("postgresql://user:pass@db:5432/app") is True

    with patch("socket.create_connection", side_effect=OSError("down")):
        assert _is_db_reachable("postgresql://user:pass@db:5432/app") is False
        with pytest.raises(RuntimeError, match="not reachable"):
            run_database_migrations("postgresql://user:pass@db:5432/app")

    mock_conn = MagicMock()
    with patch("psycopg.connect", return_value=mock_conn):
        lock_conn = _acquire_advisory_lock("postgresql+psycopg://user:pass@db:5432/app")
        assert lock_conn is mock_conn
        _release_advisory_lock(lock_conn)
        mock_conn.close.assert_called_once()


@pytest.mark.asyncio
async def test_thread_registry_redis_claim_enumeration_and_fail_closed() -> None:
    kv: dict[str, str] = {}
    sets: dict[str, set[str]] = {}

    class _FakeRedis:
        async def set(self, key: str, val: str, nx: bool = False, ex: int = 0) -> bool:
            if nx and key in kv:
                return False
            kv[key] = val
            return True

        async def get(self, key: str) -> bytes | None:
            return kv[key].encode() if key in kv else None

        async def expire(self, key: str, ttl: int) -> None:
            return None

        async def sadd(self, key: str, member: str) -> None:
            sets.setdefault(key, set()).add(member)

        async def smembers(self, key: str) -> set[bytes]:
            return {m.encode() for m in sets.get(key, set())}

        async def aclose(self) -> None:
            return None

    registry = ThreadRegistry()
    registry.settings = SimpleNamespace(
        environment="test", checkpoint_signing_key="test-checkpoint-secret"
    )
    registry._redis = _FakeRedis()

    scoped = await registry.claim("t-redis-1", "alice")
    assert scoped == registry.scoped_thread_id("t-redis-1", "alice")
    assert await registry.owner("t-redis-1") == "alice"
    assert await registry.threads_for_owner("alice") == ["t-redis-1"]

    with pytest.raises(ThreadOwnershipError):
        await registry.claim("t-redis-1", "bob")

    # Production fail-closed on Redis outage
    broken_redis = MagicMock()
    broken_redis.set = AsyncMock(side_effect=ConnectionError("redis down"))
    broken_redis.aclose = AsyncMock()
    registry.settings = SimpleNamespace(environment="production")
    registry._redis = broken_redis
    with pytest.raises(ThreadRegistryUnavailable):
        await registry.claim("t-redis-2", "alice")
