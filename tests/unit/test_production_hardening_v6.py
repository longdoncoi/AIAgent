"""Regression tests for production hardening v6 fixes."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from fastapi.testclient import TestClient

from src.agentic.a2a.cards import AgentCard
from src.agentic.a2a.executor import (
    A2AAgentExecutor,
    A2ATaskPayload,
    A2ATaskRequest,
    create_a2a_server,
)
from src.agentic.api.deps import get_current_user, get_graph
from src.agentic.core.config import get_settings
from src.agentic.core.execution_context import (
    ToolExecutionContext,
    reset_tool_execution_context,
    set_tool_execution_context,
)
from src.agentic.memory.data_lifecycle import _CHECKPOINT_SQL, _redis_patterns, purge_user_data
from src.agentic.memory.redis_saver import RedisSaver
from src.agentic.memory.thread_registry import ThreadRegistry
from src.agentic.orchestrator.nodes.hitl import hitl_node
from src.agentic.orchestrator.state import AgentState, Plan, Step
from src.agentic.rag.kg import EntityRelationTriple, KnowledgeGraphStore
from src.agentic.rag.retriever import HybridRetriever, RetrievalResult
from src.agentic.rag.tools import _authoritative_tenant_id, search_policy
from src.agentic.safety.guardrails import GuardrailManager, GuardrailViolation
from src.agentic.tools.servers.repo_server import read_file_core, search_symbol_core
from src.agentic.tools.servers.web_search_server import search_web_core
from src.agentic.workflows.rag_workflow import ingest_delta_document_activity

ROOT = Path(__file__).resolve().parents[2]
K8S = ROOT / "deploy" / "k8s"


def test_guardrails_blocks_zwsp_obfuscated_keywords() -> None:
    mgr = GuardrailManager()
    with pytest.raises(GuardrailViolation, match="DROP DATABASE"):
        mgr.sanitize_input("DROP\u200b DATABASE prod_users")
    with pytest.raises(GuardrailViolation, match="rm -rf"):
        mgr.sanitize_input("rm\u200b -rf /var/lib/data")


@pytest.mark.asyncio
async def test_hitl_node_sanitizes_modified_input(monkeypatch: pytest.MonkeyPatch) -> None:
    step = Step(
        id="step-hitl-1",
        description="Execute configuration update",
        agent="coding",
        input_query="safe initial query",
        risk_level="high",
        status="pending",
    )
    state = AgentState(
        thread_id="thr-hitl",
        plan=Plan(goal="test", steps=[step]),
    )
    monkeypatch.setattr(
        "src.agentic.orchestrator.nodes.hitl.interrupt",
        lambda payload: {"approved": True, "modified_input": "DROP\u200b DATABASE users"},
    )
    with pytest.raises(GuardrailViolation):
        await hitl_node(state)


def test_approval_response_reports_secondary_interrupt() -> None:
    from src.agentic.api.main import app

    pre_snapshot = SimpleNamespace(
        next=("hitl",),
        tasks=[SimpleNamespace(interrupts=[SimpleNamespace(value={"step_id": "step-1"})])],
    )
    post_snapshot = SimpleNamespace(
        next=("hitl",),
        tasks=[SimpleNamespace(interrupts=[SimpleNamespace(value={"step_id": "step-2"})])],
    )
    mock_graph = MagicMock()
    mock_graph.aget_state = AsyncMock(side_effect=[pre_snapshot, post_snapshot])
    mock_graph.ainvoke = AsyncMock(return_value={"final_answer": None})

    mock_registry = MagicMock()
    mock_registry.owner = AsyncMock(return_value="alice")
    mock_registry.scoped_thread_id.return_value = "scoped-thr"
    mock_registry.settings = SimpleNamespace(environment="test")

    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "alice",
        "roles": ["admin"],
        "client_id": "c1",
    }
    app.dependency_overrides[get_graph] = lambda: mock_graph
    try:
        with patch("src.agentic.api.routes.approvals.get_thread_registry", return_value=mock_registry):
            client = TestClient(app)
            resp = client.post(
                "/api/v1/approvals/thr-1/resume",
                json={
                    "thread_id": "thr-1",
                    "step_id": "step-1",
                    "decision": {"approved": True, "reason": "ok"},
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "resumed"
            assert body["requires_approval"] is True
            assert body["pending_approval_step"] == "step-2"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_graph, None)


@pytest.mark.asyncio
async def test_rtbf_purges_thread_ownership_and_sql_order() -> None:
    assert list(_CHECKPOINT_SQL.keys()) == ["checkpoint_writes", "checkpoint_blobs", "checkpoints"]

    patterns = _redis_patterns({"thread:hmac1", "pub-1"}, None, owner_id="alice")
    pub_digest = hashlib.sha256(b"pub-1").hexdigest()
    owner_digest = hashlib.sha256(b"alice").hexdigest()
    assert f"agentic:thread-owner:{pub_digest}" in patterns
    assert f"agentic:owner-threads:{owner_digest}" in patterns

    registry = ThreadRegistry()
    await registry.claim("pub-1", "alice")

    deleted_keys: list[str] = []

    class _FakeRedis:
        def scan_iter(self, match: str, count: int = 200):
            owner_key = f"agentic:owner-threads:{owner_digest}"
            thread_key = f"agentic:thread-owner:{pub_digest}"
            for candidate in (owner_key, thread_key):
                if match == candidate:
                    yield candidate.encode("utf-8")

        def delete(self, key: bytes) -> int:
            deleted_keys.append(key.decode("utf-8"))
            return 1

    fake_ltm = SimpleNamespace(
        list_by_user=lambda user_id: [],
        delete_by_user=lambda user_id: 0,
    )
    fake_cache = SimpleNamespace(invalidate_by_source=lambda source: 0)

    with (
        patch("src.agentic.memory.data_lifecycle.get_thread_registry", return_value=registry),
        patch("src.agentic.memory.data_lifecycle.record_audit_event"),
    ):
        report = await purge_user_data(
            "alice",
            redis_client=_FakeRedis(),
            long_term_store=fake_ltm,
            rag_cache=fake_cache,
        )

    assert set(deleted_keys) == {
        f"agentic:owner-threads:{owner_digest}",
        f"agentic:thread-owner:{pub_digest}",
    }
    assert report.redis_keys_deleted == 2
    assert await registry.owner("pub-1") is None
    assert await registry.threads_for_owner("alice") == []


@pytest.mark.asyncio
async def test_redis_saver_fails_closed_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    saver = RedisSaver(redis_url=None)
    monkeypatch.setattr(saver, "settings", SimpleNamespace(environment="production"))
    mock_redis = MagicMock()
    mock_redis.set.side_effect = RuntimeError("redis down")
    saver._redis_client = mock_redis

    mock_async = MagicMock()
    mock_async.set = AsyncMock(side_effect=RuntimeError("async redis down"))
    saver._async_redis_client = mock_async

    cfg = {"configurable": {"thread_id": "t1", "checkpoint_ns": "", "checkpoint_id": "c1"}}
    chk = {
        "v": 1,
        "id": "c1",
        "ts": "2026-11-25T00:00:00Z",
        "channel_values": {},
        "channel_versions": {},
        "versions_seen": {},
    }
    with pytest.raises(RuntimeError, match="Production Redis checkpoint persistence failed during put"):
        saver.put(cfg, chk, {}, {})
    with pytest.raises(
        RuntimeError, match="Production Redis checkpoint persistence failed during put_writes"
    ):
        saver.put_writes(cfg, [("k", "v")], "task-1")

    # Disable sync client so aput/aput_writes directly exercise the async Redis path.
    saver._redis_client = None
    with pytest.raises(
        RuntimeError, match="Production Redis checkpoint persistence failed during aput"
    ):
        await saver.aput(cfg, chk, {}, {})
    with pytest.raises(
        RuntimeError, match="Production Redis checkpoint persistence failed during aput_writes"
    ):
        await saver.aput_writes(cfg, [("k", "v")], "task-1")


def test_rag_fails_closed_when_degraded_and_empty() -> None:
    degraded_result = RetrievalResult([], degraded=True, degraded_reasons=["qdrant_query_failed"])
    mock_retriever = MagicMock()
    mock_retriever.retrieve.return_value = degraded_result
    mock_cache = MagicMock()
    mock_cache.get_or_compute.side_effect = lambda q, fn, **kw: fn()

    with (
        patch("src.agentic.rag.tools.get_hybrid_retriever", return_value=mock_retriever),
        patch("src.agentic.rag.tools.get_rag_cache", return_value=mock_cache),
    ):
        with pytest.raises(RuntimeError, match="RAG retrieval degraded"):
            search_policy("password policy")


def test_authoritative_tenant_prefers_explicit_tenant_id() -> None:
    tok = set_tool_execution_context(
        ToolExecutionContext(
            agent_name="rag",
            step_id="s1",
            owner_id="alice",
            tenant_id="org-acme",
        )
    )
    try:
        assert _authoritative_tenant_id() == "org-acme"
    finally:
        reset_tool_execution_context(tok)


def test_kg_thread_safety_and_bm25_cache_reuse(tmp_path: Path) -> None:
    kg = KnowledgeGraphStore(persist_path=tmp_path / "kg.json")

    def _worker(idx: int) -> None:
        kg.add_triples_batch(
            [
                EntityRelationTriple(
                    f"Policy{idx}",
                    "requires",
                    f"Control{idx}",
                    {"tenant_id": "t1"},
                )
            ]
        )
        kg.query_graph(f"Policy{idx}", filters={"tenant_id": "t1"})

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(_worker, range(24)))
    assert kg.count_tenant("t1") == 48

    retriever = HybridRetriever(index_dir=tmp_path / "idx")
    with patch.object(retriever, "_get_bm25_corpus", wraps=retriever._get_bm25_corpus) as spy:
        retriever.keyword_score("policy control")
        first_key = retriever._bm25_cache_key
        retriever.keyword_score("policy access")
        assert retriever._bm25_cache_key == first_key
        assert spy.call_count == 2
        retriever.invalidate()
        assert retriever._bm25_cache_key is None


@pytest.mark.asyncio
async def test_cdc_delete_removes_source_and_invalidates_after_rebuild(tmp_path: Path) -> None:
    raw_dir = tmp_path / "data" / "raw"
    raw_dir.mkdir(parents=True)
    target = raw_dir / "old.md"
    target.write_text("stale secret policy", encoding="utf-8")

    call_order: list[str] = []
    mock_cache = MagicMock()
    mock_cache.invalidate_document.side_effect = lambda doc_id: call_order.append("inv_doc") or 1
    mock_cache.invalidate_by_source.side_effect = lambda src: call_order.append("inv_src") or 1
    mock_cache.publish_invalidation.side_effect = lambda d, f: call_order.append("pub")

    fake_settings = SimpleNamespace(project_root=tmp_path)
    with (
        patch("src.agentic.workflows.rag_workflow.get_settings", return_value=fake_settings),
        patch("src.agentic.cache.rag_cache.get_rag_cache", return_value=mock_cache),
        patch(
            "src.agentic.workflows.rag_workflow.delete_source",
            side_effect=lambda *a, **kw: call_order.append("delete_source") or 1,
        ),
        patch(
            "src.agentic.rag.retriever.reset_hybrid_retriever",
            side_effect=lambda: call_order.append("reset_retriever"),
        ),
    ):
        res = await ingest_delta_document_activity(
            {"doc_id": "d-1", "file_path": "old.md", "action": "delete"}
        )

    assert res["status"] == "completed"
    assert not target.exists()
    assert call_order.index("delete_source") < call_order.index("reset_retriever")
    assert call_order.index("reset_retriever") < call_order.index("inv_doc")


@pytest.mark.asyncio
async def test_web_search_fails_closed_in_prod_and_repo_blocks_env_variants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prod_settings = SimpleNamespace(is_production=True, web_search_api_url="", web_search_api_key="")
    with (
        patch("src.agentic.core.config.get_settings", return_value=prod_settings),
        patch("httpx.AsyncClient.post", side_effect=OSError("egress blocked")),
    ):
        with pytest.raises(RuntimeError, match="Live web search unavailable in production"):
            await search_web_core("latest cve")

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / ".env.production").write_text("SECRET=123\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("EXAMPLE=1\n", encoding="utf-8")
    big_file = tmp_path / "huge.log"
    big_file.write_bytes(b"a" * (2 * 1024 * 1024 + 64))

    assert "Security Violation" in read_file_core(".env.production")
    assert "EXAMPLE=1" in read_file_core(".env.example")
    assert "read size limit" in read_file_core("huge.log")
    assert "No matches found" in search_symbol_core("aaaa", ".")


def test_a2a_ready_offloads_and_stream_creates_otel_span(monkeypatch: pytest.MonkeyPatch) -> None:
    card = AgentCard(name="chat_agent", description="chat", url="http://localhost:9100")

    class _FakeGraph:
        async def ainvoke(self, state, config=None):
            return state

        async def astream(self, state, config=None, stream_mode=None):
            yield {"messages": []}

    settings = get_settings()
    monkeypatch.setattr(settings, "require_model_gateway_ready", True)
    app = create_a2a_server(card, _FakeGraph())
    executor = A2AAgentExecutor(card, _FakeGraph())

    span_cm = MagicMock()
    mock_span = MagicMock()
    span_cm.__enter__.return_value = mock_span
    span_cm.__exit__.return_value = False
    mock_tracer = MagicMock()
    mock_tracer.start_as_current_span.return_value = span_cm

    with (
        patch("src.agentic.a2a.executor._tcp_reachable", return_value=True) as mock_tcp,
        patch("src.agentic.a2a.executor.trace.get_tracer", return_value=mock_tracer),
    ):
        client = TestClient(app)
        ready_resp = client.get("/ready")
        assert ready_resp.status_code == 200
        mock_tcp.assert_called_once()

        stream_resp = client.post(
            "/a2a/tasks/stream",
            headers={"X-Internal-Service-Token": settings.s2s_internal_token},
            json={
                "task_id": "t-stream",
                "caller": "supervisor",
                "target_agent": "chat_agent",
                "payload": {"instruction": "hello"},
                "metadata": {"tenant_id": "tenant-xyz"},
            },
        )
        assert stream_resp.status_code == 200
        mock_tracer.start_as_current_span.assert_called()

    ctx = executor._execution_context(
        A2ATaskRequest(
            task_id="t-ctx",
            caller="supervisor",
            target_agent="chat_agent",
            payload=A2ATaskPayload(instruction="hi"),
            metadata={"owner_id": "alice", "tenant_id": "org-1"},
        )
    )
    assert ctx.tenant_id == "org-1"


def test_k8s_istio_netpol_and_eso_invariants() -> None:
    istio_docs = list(yaml.safe_load_all((K8S / "istio-security.yaml").read_text(encoding="utf-8")))
    by_name = {d["metadata"]["name"]: d for d in istio_docs if d}
    api_principals = by_name["allow-ingress-to-api"]["spec"]["rules"][0]["from"][0]["source"]["principals"]
    assert "cluster.local/ns/ingress-nginx/sa/ingress-nginx" in api_principals
    sub_principals = by_name["allow-api-to-subagents"]["spec"]["rules"][0]["from"][0]["source"]["principals"]
    assert "cluster.local/ns/agentic-platform/sa/agentic-temporal-worker-sa" in sub_principals
    assert by_name["allow-api-to-subagents"]["spec"]["rules"][1]["from"][0]["source"]["namespaces"] == [
        "monitoring"
    ]

    netpol_docs = list(yaml.safe_load_all((K8S / "network-policy.yaml").read_text(encoding="utf-8")))
    np_by_name = {d["metadata"]["name"]: d for d in netpol_docs if d}
    sub_ingress_from = np_by_name["allow-subagents"]["spec"]["ingress"][0]["from"]
    assert {"podSelector": {"matchLabels": {"app": "agentic-temporal-worker"}}} in sub_ingress_from
    assert {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "monitoring"}}
    } in sub_ingress_from

    eso_docs = list(yaml.safe_load_all((K8S / "external-secret.yaml").read_text(encoding="utf-8")))
    eso_by_name = {d["metadata"]["name"]: d for d in eso_docs if d and d["kind"] == "ExternalSecret"}
    api_keys = {e["secretKey"] for e in eso_by_name["agentic-api-secrets"]["spec"]["data"]}
    assert {"REDIS_URL", "CHECKPOINT_HMAC_SECRET"}.issubset(api_keys)
    assert "REDIS_URL" in {e["secretKey"] for e in eso_by_name["agentic-rag-secrets"]["spec"]["data"]}
    assert "REDIS_URL" in {
        e["secretKey"] for e in eso_by_name["agentic-temporal-secrets"]["spec"]["data"]
    }


def test_python315_dataclasses_compat_and_dockerfile_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    import dataclasses

    from src.agentic.rag.reranker import _ensure_python315_dataclasses_compat

    monkeypatch.delattr(dataclasses, "_MISSING_TYPE", raising=False)
    assert not hasattr(dataclasses, "_MISSING_TYPE")
    _ensure_python315_dataclasses_compat()
    assert dataclasses._MISSING_TYPE is type(dataclasses.MISSING)  # type: ignore[attr-defined]

    agent_df = (ROOT / "deploy" / "docker" / "Dockerfile.agent").read_text(encoding="utf-8")
    assert "_py315_dataclasses_compat.pth" in agent_df
    assert "from sentence_transformers import CrossEncoder" in agent_df
    for suffix in ("api", "mcp"):
        df = (ROOT / "deploy" / "docker" / f"Dockerfile.{suffix}").read_text(encoding="utf-8")
        assert "--extra neural" not in df
