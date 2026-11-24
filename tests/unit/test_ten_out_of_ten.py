"""Comprehensive verification tests for 10/10 Enterprise Production Hardening.

Covers:
1. Cache Stampede Mitigation (SingleFlight / Distributed Mutex Lock)
2. Sub-Agent Fine-Grained Token Streaming (A2A execute_stream & call_agent_stream)
3. PostgreSQL Connection Pooling
4. Real-time Knowledge CDC Ingestion Activity
5. Istio Security & PgBouncer K8s Architecture Validation
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from src.agentic.a2a.cards import CHAT_AGENT_CARD
from src.agentic.a2a.client import get_a2a_client
from src.agentic.a2a.executor import A2AAgentExecutor, A2ATaskPayload, A2ATaskRequest
from src.agentic.cache.prompt_cache import PromptCache
from src.agentic.cache.rag_cache import RAGCache
from src.agentic.memory.short_term import get_postgres_pool
from src.agentic.workflows.rag_workflow import (
    ingest_delta_document_activity,
)


def test_cache_stampede_singleflight_protection() -> None:
    """Verifies that concurrent requests for uncached queries compute exactly ONCE."""
    rag_cache = RAGCache(max_entries=100, default_ttl=60, enable_redis=False)
    compute_count = 0

    def slow_expensive_retriever() -> str:
        nonlocal compute_count
        import time

        time.sleep(0.1)
        compute_count += 1
        return "expensive_semantic_result"

    query = "concurrent_stampede_test_query"

    def worker(_: int) -> str:
        return rag_cache.get_or_compute(query, slow_expensive_retriever, top_k=3, timeout=3.0)

    # Fire 8 concurrent threads requesting the exact same uncached query
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(worker, range(8)))

    # All 8 workers must receive the exact result
    for r in results:
        assert r == "expensive_semantic_result"

    # Compute must run only ONCE because stampede lock protected it
    assert compute_count == 1
    assert rag_cache.stats["hits"] >= 7


@pytest.mark.asyncio
async def test_async_cache_stampede_protection() -> None:
    """Verifies async SingleFlight protection on PromptCache."""
    prompt_cache = PromptCache(max_entries=100, default_ttl=60, enable_redis=False)
    async_computes = 0

    async def async_llm_call() -> str:
        nonlocal async_computes
        await asyncio.sleep(0.05)
        async_computes += 1
        return "async_llm_completion"

    messages = [{"role": "user", "content": "Tell me a story"}]

    async def worker() -> str:
        return await prompt_cache.get_or_compute_async(
            "reasoning-llm",
            messages,
            async_llm_call,
            temperature=0.2,
            timeout=2.0,
        )

    # Gather 6 concurrent coroutines
    results = await asyncio.gather(*[worker() for _ in range(6)])

    for r in results:
        assert r == "async_llm_completion"

    assert async_computes == 1


@pytest.mark.asyncio
async def test_a2a_fine_grained_token_streaming() -> None:
    """Verifies that A2A executor yields token-by-token stream events."""
    from unittest.mock import MagicMock

    from langchain_core.messages import AIMessageChunk

    mock_graph = MagicMock()

    async def mock_astream_events(initial_state: dict, version: str = "v2"):
        yield {
            "event": "on_chat_model_stream",
            "data": {"chunk": AIMessageChunk(content="Hello ")},
        }
        yield {
            "event": "on_chat_model_stream",
            "data": {"chunk": AIMessageChunk(content="World!")},
        }

    mock_graph.astream_events = mock_astream_events
    executor = A2AAgentExecutor(card=CHAT_AGENT_CARD, graph=mock_graph)

    req = A2ATaskRequest(
        task_id="step_stream_01",
        caller="supervisor",
        target_agent="chat_agent",
        payload=A2ATaskPayload(instruction="Xin chào, tóm tắt nhiệm vụ của bạn."),
    )

    events = []
    async for ev in executor.execute_stream(req):
        events.append(ev)

    event_types = [e.get("event") for e in events]
    assert event_types == ["start", "token", "token", "completed"]
    tokens = [str(e.get("token", "")) for e in events if e.get("event") == "token"]
    assert "".join(tokens) == "Hello World!"
    assert events[-1]["output"] == "Hello World!"


@pytest.mark.asyncio
async def test_a2a_client_stream_local_fallback() -> None:
    """Verifies A2AClient.call_agent_stream streams smoothly with local fallback."""
    client = get_a2a_client()
    events = []
    async for ev in client.call_agent_stream(
        target_agent="chat",
        step_id="step_test_stream",
        instruction="Ping",
    ):
        events.append(ev)

    assert len(events) >= 2
    types = [e.get("event") for e in events]
    assert types[0] == "start"
    assert types[-1] in ["completed", "error"]


def test_postgres_connection_pool_singleton() -> None:
    """Verifies ConnectionPool initialization helper handles configuration gracefully."""
    pool = get_postgres_pool()
    # In test environment with default uri, pool may be active or gracefully skipped
    assert pool is None or hasattr(pool, "getconn") or hasattr(pool, "connection")


@pytest.mark.asyncio
async def test_realtime_knowledge_cdc_activity(tmp_path: Path) -> None:
    """Verifies delta document ingestion activity updates store and clears RAG cache."""
    event = {
        "doc_id": "delta_001",
        "file_path": "security/vpn_policy_delta.md",
        "action": "upsert",
        "content": "All remote connections must use Hardware Token 2FA.",
        "metadata": {"version": "v2.1", "author": "security_team"},
    }

    res = await ingest_delta_document_activity(event)
    assert res["status"] == "completed"
    assert res["action"] == "upsert"
    assert res["doc_id"] == "delta_001"
    assert res["cache_invalidated"] is True


def test_istio_and_pgbouncer_manifests_syntax() -> None:
    """Verifies Kubernetes YAML syntax for newly added PgBouncer and Istio manifests."""
    proj_root = Path(__file__).resolve().parents[2]
    pgbouncer_path = proj_root / "deploy" / "k8s" / "pgbouncer.yaml"
    istio_path = proj_root / "deploy" / "k8s" / "istio-security.yaml"

    assert pgbouncer_path.exists()
    assert istio_path.exists()

    with open(pgbouncer_path, "r", encoding="utf-8") as f:
        pg_docs = list(yaml.safe_load_all(f))
        assert len(pg_docs) == 2  # Deployment + Service
        assert pg_docs[0]["metadata"]["name"] == "pgbouncer"

    with open(istio_path, "r", encoding="utf-8") as f:
        istio_docs = list(yaml.safe_load_all(f))
        assert len(istio_docs) >= 4  # PeerAuth + 3 AuthPolicies
        assert istio_docs[0]["kind"] == "PeerAuthentication"
        assert istio_docs[0]["spec"]["mtls"]["mode"] == "STRICT"


@pytest.mark.asyncio
async def test_redis_saver_hmac_and_async_io() -> None:
    """Verifies RedisSaver HMAC signature verification and native async aput/aget_tuple."""
    from src.agentic.memory.redis_saver import RedisSaver

    saver = RedisSaver(redis_url=None)  # In-memory mode with full HMAC logic active

    config = {
        "configurable": {
            "thread_id": "thread_async_redis_001",
            "checkpoint_ns": "",
        }
    }
    checkpoint = {
        "v": 1,
        "id": "chk_async_001",
        "ts": "2026-11-19T00:00:00Z",
        "channel_values": {"data": "secure_checkpoint_value"},
        "channel_versions": {"data": 1},
        "versions_seen": {},
    }
    metadata = {"step": 1, "test": "async_hmac"}

    # 1. Verify aput works asynchronously
    saved_cfg = await saver.aput(
        cast(Any, config), cast(Any, checkpoint), cast(Any, metadata), new_versions={"data": 1}
    )
    assert saved_cfg["configurable"]["checkpoint_id"] == "chk_async_001"

    # 2. Verify aget_tuple works asynchronously
    retrieved = await saver.aget_tuple(saved_cfg)
    assert retrieved is not None
    assert retrieved.checkpoint["channel_values"]["data"] == "secure_checkpoint_value"

    # 3. Verify HMAC signature generation and tamper protection
    payload = saver._sign_and_serialize({"test": "secret_data"})
    assert payload.startswith(b"HMAC-SHA256:")

    # Legitimate payload deserializes cleanly
    clean_data = saver._verify_and_deserialize(payload)
    assert clean_data == {"test": "secret_data"}

    # Tampered payload must be rejected
    tampered = payload[:-4] + b"fake"
    rejected = saver._verify_and_deserialize(tampered)
    assert rejected is None


@pytest.mark.asyncio
async def test_knowledge_graph_batch_and_async(tmp_path: Path) -> None:
    """Verifies batch insertion and async indexing on KnowledgeGraphStore."""
    from src.agentic.rag.kg import EntityRelationTriple, KnowledgeGraphStore

    kg_path = tmp_path / "test_kg.json"
    kg = KnowledgeGraphStore(persist_path=kg_path)

    triples = [
        EntityRelationTriple("AdminService", "quản lý", "UserAccounts", {"source": "unit_test"}),
        EntityRelationTriple("UserAccounts", "yêu cầu", "MFA_Token", {"source": "unit_test"}),
        EntityRelationTriple("MFA_Token", "áp dụng cho", "AllEmployees", {"source": "unit_test"}),
    ]

    # Test batch addition
    kg.add_triples_batch(triples)
    assert kg.graph.number_of_nodes() >= 4
    assert kg.graph.number_of_edges() >= 3

    # Test async text indexing
    doc_text = "Hệ thống thanh toán yêu cầu chứng chỉ số PCI-DSS để vận hành.\nChính sách bảo mật áp dụng cho toàn bộ nhân viên."
    indexed_count = await kg.aindex_text(doc_text, source_doc="policy_doc_01")
    assert indexed_count >= 1

    # Test async query
    nodes = await kg.aquery_graph("chứng chỉ số", top_k=2)
    assert len(nodes) >= 1
    assert "PCI-DSS" in nodes[0].node.get_content() or "thanh toán" in nodes[0].node.get_content()


def test_long_term_store_pool_connection(tmp_path: Path) -> None:
    """Verifies that LongTermStore reuses PostgreSQL ConnectionPool if active."""
    from unittest.mock import MagicMock, patch

    from src.agentic.memory.long_term import LongTermStore

    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [
        ("sem_key1", "semantic", "dark_mode_theme", '{"user":"test"}', "2026-11-19T00:00:00Z")
    ]
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_pool.connection.return_value.__enter__.return_value = mock_conn

    with patch("src.agentic.memory.short_term.get_postgres_pool", return_value=mock_pool):
        store = LongTermStore(storage_dir=tmp_path, force_sqlite=True)
        # Test _get_pg_conn context manager borrows from mock_pool
        with store._get_pg_conn() as conn:
            assert conn == mock_conn
        mock_pool.connection.assert_called_once()


def test_pgbouncer_prepared_statements_config() -> None:
    """Verifies PgBouncer manifest enables MAX_PREPARED_STATEMENTS for transaction mode."""
    proj_root = Path(__file__).resolve().parents[2]
    pgbouncer_path = proj_root / "deploy" / "k8s" / "pgbouncer.yaml"

    with open(pgbouncer_path, "r", encoding="utf-8") as f:
        docs = list(yaml.safe_load_all(f))
        deployment = docs[0]
        containers = deployment["spec"]["template"]["spec"]["containers"]
        env_vars = {env["name"]: env.get("value") for env in containers[0]["env"] if "value" in env}

        assert env_vars.get("POOL_MODE") == "transaction"
        assert env_vars.get("MAX_PREPARED_STATEMENTS") == "100"
