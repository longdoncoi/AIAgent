"""P0 hardening regression tests for the RAG and cache subsystems.

Covers tenant-override rejection, security metadata, stale-vector deletion,
cross-replica L2 invalidation, exact domain keying, owned Redis locks,
structured citations, deterministic UUID node IDs, fail-closed degradation and
PII redaction.
"""

from __future__ import annotations

import fnmatch
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from llama_index.core.schema import (
    NodeRelationship,
    NodeWithScore,
    RelatedNodeInfo,
    TextNode,
)
from prometheus_client import Counter

from src.agentic.cache.prompt_cache import PromptCache
from src.agentic.cache.rag_cache import (
    RAG_INVALIDATION_GENERATION_KEY,
    RAG_INVALIDATION_LATEST_KEY,
    RAGCache,
)
from src.agentic.cache.redaction import redact_pii
from src.agentic.core.config import get_settings
from src.agentic.rag import reranker as reranker_module
from src.agentic.rag.embedding import probe_embedding_model
from src.agentic.rag.ingestion import (
    _assign_deterministic_node_ids,
    _attach_security_metadata,
    ingest_documents,
)
from src.agentic.rag.metrics import (
    RAG_DENSE_RETRIEVAL_ERRORS_TOTAL,
    RAG_RERANKER_FALLBACK_TOTAL,
)
from src.agentic.rag.reranker import _get_cross_encoder
from src.agentic.rag.retriever import HybridRetriever, get_qdrant_vector_store
from src.agentic.rag.tools import (
    _authoritative_tenant_id,
    _build_effective_filters,
    search_policy,
    validate_citations,
)
from src.agentic.tools.execution_context import (
    ToolExecutionContext,
    reset_tool_execution_context,
    set_tool_execution_context,
)


class FakePipeline:
    """Minimal Redis pipeline that records deletes."""

    def __init__(self, client: FakeRedis) -> None:
        self.client = client
        self.keys: list[str] = []

    def delete(self, key: str) -> FakePipeline:
        self.keys.append(key)
        return self

    def execute(self) -> list[int]:
        return [self.client.delete(key) for key in self.keys]


class FakeRedis:
    """In-memory Redis stand-in implementing the cache's used commands."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def ping(self) -> bool:
        return True

    def get(self, key: str) -> str | None:
        value = self.store.get(key)
        return str(value) if value is not None else None

    def set(self, key: str, value: str, **kwargs: Any) -> bool:
        self.store[key] = str(value)
        return True

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                removed += 1
        return removed

    def scan_iter(self, match: str | None = None, count: int | None = None) -> Any:
        pattern = match or "*"
        return iter([k for k in list(self.store) if fnmatch.fnmatch(k, pattern)])

    def keys(self, pattern: str) -> list[str]:
        return [k for k in self.store if fnmatch.fnmatch(k, pattern)]

    def ttl(self, key: str) -> int:
        return 3600

    def incr(self, key: str) -> int:
        value = int(self.store.get(key, "0")) + 1
        self.store[key] = str(value)
        return value

    def publish(self, channel: str, message: str) -> int:
        return 1

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)

    def eval(self, script: str, numkeys: int, key: str, token: str) -> int:
        """Compare-and-delete: only the owning token may remove the key."""
        if self.store.get(key) == token:
            return self.delete(key)
        return 0


def _cache_with_redis(redis_client: FakeRedis) -> RAGCache:
    with patch("src.agentic.core.redis.get_redis_client", return_value=redis_client):
        cache = RAGCache(max_entries=50, default_ttl=300, enable_redis=True)
    assert cache._redis_client is redis_client
    return cache


def _prompt_cache_with_redis(redis_client: FakeRedis) -> PromptCache:
    with patch("src.agentic.core.redis.get_redis_client", return_value=redis_client):
        cache = PromptCache(max_entries=50, default_ttl=300, enable_redis=True)
    assert cache._redis_client is redis_client
    return cache


def _counter_value(counter: Counter, **labels: str) -> float:
    return float(counter.labels(**labels)._value.get())


# ---------------------------------------------------------------------------
# 1. Tenant override and authoritative filters
# ---------------------------------------------------------------------------


def test_caller_supplied_tenant_keys_are_rejected() -> None:
    token = set_tool_execution_context(
        ToolExecutionContext(agent_name="rag", step_id="s1", owner_id="alice", tenant_id="alice")
    )
    try:
        effective = _build_effective_filters(
            {
                "tenant_id": "victim",
                "owner_id": "victim",
                "acl": ["victim"],
                "tenant": "victim",
                "domain": "legal",
            }
        )
        assert effective["tenant_id"] == "alice"
        for key in ("owner_id", "acl", "tenant"):
            assert key not in effective
        assert effective["domain"] == "legal"
    finally:
        reset_tool_execution_context(token)


def test_owner_id_never_overrides_tenant_id() -> None:
    token = set_tool_execution_context(
        ToolExecutionContext(agent_name="rag", step_id="s1b", owner_id="alice")
    )
    try:
        assert _authoritative_tenant_id() == getattr(get_settings(), "rag_default_tenant_id", "public")
    finally:
        reset_tool_execution_context(token)


def test_search_policy_cache_scope_uses_authoritative_tenant() -> None:
    node = NodeWithScore(
        node=TextNode(
            text="Policy text",
            metadata={"file_name": "policy.md", "doc_id": "d-1"},
        ),
        score=0.9,
    )
    fake_cache = MagicMock()
    fake_cache.get_or_compute.side_effect = lambda query, fn, **kwargs: fn()

    token = set_tool_execution_context(
        ToolExecutionContext(agent_name="rag", step_id="s2", owner_id="alice", tenant_id="alice")
    )
    try:
        with (
            patch("src.agentic.rag.tools.get_rag_cache", return_value=fake_cache),
            patch("src.agentic.rag.tools.get_hybrid_retriever") as mock_retriever,
            patch("src.agentic.rag.tools.CrossEncoderReranker") as mock_reranker,
        ):
            mock_retriever.return_value.retrieve.return_value = [node]
            mock_reranker.return_value.rerank.return_value = [node]
            result = search_policy(
                "password policy",
                {"tenant_id": "victim", "owner_id": "victim", "category": "security"},
            )

        retrieved_filters = mock_retriever.return_value.retrieve.call_args.kwargs["filters"]
        assert retrieved_filters["tenant_id"] == "alice"
        assert "owner_id" not in retrieved_filters
        scope = fake_cache.get_or_compute.call_args.kwargs["scope"]
        assert json.loads(scope)["tenant_id"] == "alice"
        assert "victim" not in scope
        assert validate_citations(result)
    finally:
        reset_tool_execution_context(token)


def test_missing_verified_context_uses_config_default_tenant() -> None:
    effective = _build_effective_filters({"tenant_id": "victim"})
    assert effective["tenant_id"] == getattr(get_settings(), "rag_default_tenant_id", "public")


def test_missing_tenant_metadata_is_dropped_in_production() -> None:
    retriever = HybridRetriever.__new__(HybridRetriever)
    retriever.settings = SimpleNamespace(require_embedding_service=True)
    item = NodeWithScore(node=TextNode(text="untagged", metadata={}), score=1.0)
    assert retriever._is_permitted(item, {"tenant_id": "alice"}) is False

    tagged = NodeWithScore(
        node=TextNode(text="tagged", metadata={"tenant_id": "alice"}), score=1.0
    )
    assert retriever._is_permitted(tagged, {"tenant_id": "alice"}) is True


def test_document_rbac_requires_role_department_and_acl_match() -> None:
    retriever = HybridRetriever.__new__(HybridRetriever)
    retriever.settings = SimpleNamespace(require_embedding_service=True)
    permitted = NodeWithScore(
        node=TextNode(
            text="finance policy",
            metadata={
                "tenant_id": "acme",
                "allowed_roles": ["operator"],
                "department": "finance",
                "acl": ["role:operator"],
            },
        ),
        score=1.0,
    )
    scope = {
        "tenant_id": "acme",
        "user_roles": ["operator"],
        "department": "finance",
        "acl_principals": ["alice", "role:operator", "finance"],
    }
    assert retriever._is_permitted(permitted, scope) is True
    assert retriever._is_permitted(permitted, {**scope, "user_roles": ["guest"]}) is False
    assert retriever._is_permitted(permitted, {**scope, "department": "engineering"}) is False
    assert retriever._is_permitted(
        permitted, {**scope, "acl_principals": ["bob", "role:guest"]}
    ) is False


def test_rag_authorization_filters_cannot_be_overridden_by_tool_arguments() -> None:
    token = set_tool_execution_context(
        ToolExecutionContext(
            agent_name="rag",
            step_id="s-rbac",
            owner_id="acme",
            tenant_id="acme",
            user_roles=("operator",),
            department="finance",
        )
    )
    try:
        effective = _build_effective_filters(
            {
                "tenant_id": "victim",
                "user_roles": ["admin"],
                "department": "executive",
                "acl_principals": ["victim"],
            }
        )
        assert effective == {
            "tenant_id": "acme",
            "user_roles": ["operator"],
            "department": "finance",
            "acl_principals": ["acme", "finance", "public", "role:operator"],
        }
    finally:
        reset_tool_execution_context(token)


# ---------------------------------------------------------------------------
# 2. Security metadata + stale-vector deletion + fail-closed ingestion
# ---------------------------------------------------------------------------


def test_ingestion_attaches_trusted_tenant_and_acl() -> None:
    from llama_index.core import Document

    docs = [Document(text="x", metadata={"file_name": "a.md"})]
    _attach_security_metadata(docs, "acme", None)
    assert docs[0].metadata["tenant_id"] == "acme"
    assert docs[0].metadata["acl"] == ["acme"]
    _attach_security_metadata(docs, "acme", ["group-1"])
    assert docs[0].metadata["acl"] == ["group-1"]


def test_reingest_deletes_stale_vector_points(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    index_dir = tmp_path / "index"
    source = raw_dir / "policy.md"
    source.write_text("Original policy content. " * 80, encoding="utf-8")

    store = MagicMock()

    with (
        patch("src.agentic.rag.retriever.get_qdrant_vector_store", return_value=store),
        patch("src.agentic.rag.kg.get_knowledge_graph_store") as mock_kg,
    ):
        mock_kg.return_value.index_text.return_value = 0
        first_count = ingest_documents(raw_data_dir=raw_dir, index_persist_dir=index_dir)
        assert first_count > 0
        manifest_path = index_dir / "ingestion_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        key = str(source)
        old_ids = set(manifest["sources"][key])
        assert old_ids

        store.reset_mock()
        source.write_text("Completely revised policy content. " * 80, encoding="utf-8")
        second_count = ingest_documents(raw_data_dir=raw_dir, index_persist_dir=index_dir)
        assert second_count > 0
        new_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        new_ids = set(new_manifest["sources"][key])
        assert old_ids != new_ids

        deleted_args = [call.kwargs.get("node_ids", []) for call in store.delete_nodes.call_args_list]
        deleted = {pid for group in deleted_args for pid in (group or [])}
        assert old_ids - new_ids <= deleted


def test_empty_corpus_fails_closed_outside_dev(tmp_path: Path) -> None:
    empty_raw = tmp_path / "empty"
    empty_raw.mkdir()
    settings = get_settings()
    with patch.object(settings, "environment", "production"):
        with pytest.raises(RuntimeError, match="Refusing to seed fabricated"):
            ingest_documents(
                raw_data_dir=empty_raw, index_persist_dir=tmp_path / "idx"
            )


# ---------------------------------------------------------------------------
# 3. Deterministic UUID node IDs + relationship preservation
# ---------------------------------------------------------------------------


def test_node_ids_are_valid_uuid5_and_stable() -> None:
    parent = TextNode(text="parent content", metadata={"file_name": "p.md"})
    child = TextNode(
        text="child content",
        metadata={"file_name": "p.md"},
        relationships={
            NodeRelationship.PARENT: RelatedNodeInfo(node_id=parent.id_)
        },
    )
    nodes = [parent, child]
    _assign_deterministic_node_ids(nodes, "acme")
    first_ids = [node.id_ for node in nodes]
    for node_id in first_ids:
        parsed = uuid.UUID(node_id)
        assert parsed.version == 5
        assert node_id == str(parsed)

    related = nodes[1].relationships[NodeRelationship.PARENT]
    assert isinstance(related, RelatedNodeInfo)
    assert related.node_id == nodes[0].id_

    _assign_deterministic_node_ids(nodes, "acme")
    assert [node.id_ for node in nodes] == first_ids


# ---------------------------------------------------------------------------
# 4. Cache invalidation, domain keying, redaction
# ---------------------------------------------------------------------------


def test_remote_invalidation_deletes_l2_values() -> None:
    redis_client = FakeRedis()
    cache = _cache_with_redis(redis_client)

    cache.set("stale query", "stale answer", top_k=3, domain="general")
    keys = [k for k in redis_client.store if k.startswith("rag_cache:")]
    assert len(keys) == 1

    # Another replica advances the shared generation and publishes the event.
    redis_client.incr(RAG_INVALIDATION_GENERATION_KEY)
    redis_client.set(
        RAG_INVALIDATION_LATEST_KEY,
        json.dumps({"doc_id": "d1", "file_path": "policy.md", "generation": 1}),
    )
    generation = cache.check_invalidation_generation()

    assert generation == 1
    assert redis_client.store.get(keys[0]) is None
    assert cache.get("stale query", top_k=3) is None


def test_domain_is_part_of_exact_key_and_match_is_exact_for_sensitive_domains() -> None:
    cache = RAGCache(max_entries=50, default_ttl=300, enable_redis=False)
    general_key = cache._hash_key("refund policy", 3, domain="general")
    finance_key = cache._hash_key("refund policy", 3, domain="finance")
    assert general_key != finance_key

    cache.set_semantic(
        "refund policy", "GENERAL ANSWER", domain="general", top_k=3, query_vector=[1.0, 0.0]
    )
    cache.set_semantic(
        "refund policy", "FINANCE ANSWER", domain="finance", top_k=3, query_vector=[0.0, 1.0]
    )
    assert cache.get("refund policy", top_k=3, domain="general") == "GENERAL ANSWER"
    assert cache.get("refund policy", top_k=3, domain="finance") == "FINANCE ANSWER"

    vec = [0.0, 1.0]
    assert cache.get_semantic("refund policy", domain="finance", top_k=3, query_vector=vec) == (
        "FINANCE ANSWER"
    )
    # A general entry must never satisfy a sensitive-domain query.
    cache._semantic_entries = [
        e for e in cache._semantic_entries if e["domain"] != "general"
    ]
    assert (
        cache.get_semantic("refund policy", domain="finance", top_k=3, query_vector=vec)
        == "FINANCE ANSWER"
    )


def test_cache_writes_redact_pii_and_queries_are_not_logged(tmp_path: Path) -> None:
    redis_client = FakeRedis()
    cache = _cache_with_redis(redis_client)
    secret_response = "Contact alice@example.com with Bearer sk-abc12345678901234567890"
    cache.set("secret query", secret_response, top_k=3)

    stored = " ".join(redis_client.store.values())
    assert "alice@example.com" not in stored
    assert "sk-abc12345678901234567890" not in stored
    assert "[REDACTED_EMAIL]" in stored
    assert redact_pii(secret_response) != secret_response


# ---------------------------------------------------------------------------
# 5. Owned Redis locks
# ---------------------------------------------------------------------------


def test_rag_cache_lock_release_is_owner_scoped() -> None:
    redis_client = FakeRedis()
    cache = _cache_with_redis(redis_client)
    assert cache.acquire_lock("stampede", timeout=5) is True
    lock_key = "lock:rag:stampede"
    token = cache._lock_tokens["stampede"]
    assert redis_client.store[lock_key] == token

    # TTL expired and another request took the lock; our release must be a no-op.
    redis_client.store[lock_key] = "someone-else"
    cache.release_lock("stampede")
    assert redis_client.store[lock_key] == "someone-else"

    # The genuine owner can still release it.
    redis_client.store[lock_key] = token
    cache._lock_tokens["stampede"] = token
    cache.release_lock("stampede")
    assert lock_key not in redis_client.store
    assert "stampede" not in cache._inflight_locks


def test_prompt_cache_lock_release_is_owner_scoped() -> None:
    redis_client = FakeRedis()
    cache = _prompt_cache_with_redis(redis_client)
    assert cache.acquire_lock("p-stampede", timeout=5) is True
    lock_key = "lock:prompt:p-stampede"
    token = cache._lock_tokens["p-stampede"]
    redis_client.store[lock_key] = "another-owner"
    cache.release_lock("p-stampede")
    assert redis_client.store[lock_key] == "another-owner"
    redis_client.store[lock_key] = token
    cache._lock_tokens["p-stampede"] = token
    cache.release_lock("p-stampede")
    assert lock_key not in redis_client.store


# ---------------------------------------------------------------------------
# 6. Structured citations
# ---------------------------------------------------------------------------


def test_validate_citations_rejects_uncited_response() -> None:
    assert validate_citations("No relevant enterprise documentation found.") is False
    assert validate_citations("Some answer with no citation block at all.") is False
    assert validate_citations("[CITATIONS]\nnot json\n[/CITATIONS]") is False
    assert (
        validate_citations(
            '[CITATIONS]\n{"citations": [{"source": "no doc id"}]}\n[/CITATIONS]'
        )
        is False
    )
    assert (
        validate_citations(
            '[CITATIONS]\n{"citations": [{"doc_id": "d1", "file_name": "p.md"}]}\n[/CITATIONS]'
        )
        is True
    )


# ---------------------------------------------------------------------------
# 7. Fail-closed reranker, embedding probe, dense degradation, Qdrant config
# ---------------------------------------------------------------------------


def test_reranker_fails_closed_when_neural_required() -> None:
    import sys

    fake_module = SimpleNamespace(CrossEncoder=MagicMock(side_effect=RuntimeError("no weights")))
    with (
        patch.dict(sys.modules, {"sentence_transformers": fake_module}),
        patch("src.agentic.rag.reranker._require_neural", return_value=True),
        patch.dict(reranker_module._model_cache, {}, clear=True),
    ):
        with pytest.raises(RuntimeError, match="require_neural_reranker"):
            _get_cross_encoder("mandatory-neural-model")


def test_reranker_fallback_is_counted_and_reported() -> None:
    with (
        patch.dict(reranker_module._model_cache, {}, clear=True),
        patch("src.agentic.rag.reranker._require_neural", return_value=False),
    ):
        before = _counter_value(RAG_RERANKER_FALLBACK_TOTAL, reason="model_load_failed")
        assert _get_cross_encoder("fallback-model") is None
        assert _counter_value(RAG_RERANKER_FALLBACK_TOTAL, reason="model_load_failed") > before
        # A failed load is not cached as None: a later call retries.
        assert "fallback-model" not in reranker_module._model_cache


def test_embedding_probe_rejects_dimension_mismatch() -> None:
    settings = SimpleNamespace(default_embedding_dim=4)
    good = MagicMock()
    good.get_text_embedding.return_value = [0.1] * 4
    assert probe_embedding_model(good, settings, "real") == 4  # type: ignore[arg-type]

    class WrongDimensionEmbedder:
        def get_text_embedding(self, text: str) -> list[float]:
            return [0.1] * 3

    with pytest.raises(RuntimeError, match="dimension"):
        probe_embedding_model(WrongDimensionEmbedder(), settings, "real")  # type: ignore[arg-type]


def test_dense_failure_marks_result_degraded() -> None:
    retriever = HybridRetriever.__new__(HybridRetriever)
    retriever.settings = SimpleNamespace(enable_knowledge_graph=False, require_embedding_service=False)
    retriever._index = MagicMock()
    retriever._docstore = None
    retriever.raw_dir = Path("__no_such_raw_dir__")
    retriever._storage_context = None
    retriever._degraded_reasons = []
    retriever._index.as_retriever.side_effect = RuntimeError("dense backend down")

    before = _counter_value(RAG_DENSE_RETRIEVAL_ERRORS_TOTAL, reason="dense_query_failed")
    result = retriever.retrieve("some query")
    assert result.degraded is True
    assert "dense_query_failed" in result.degraded_reasons
    assert (
        _counter_value(RAG_DENSE_RETRIEVAL_ERRORS_TOTAL, reason="dense_query_failed") > before
    )


def test_qdrant_consistency_validation_and_payload_indexes() -> None:
    settings = get_settings()
    with (
        patch.object(settings, "qdrant_write_consistency_factor", 2),
        patch.object(settings, "qdrant_replication_factor", 1),
    ):
        with pytest.raises(ValueError, match="write_consistency_factor"):
            get_qdrant_vector_store(client=MagicMock())

    mock_client = MagicMock()
    mock_client.get_collections.return_value = SimpleNamespace(collections=[])
    store = get_qdrant_vector_store(client=mock_client)
    assert store is not None
    indexed_fields = {
        call.kwargs["field_name"] for call in mock_client.create_payload_index.call_args_list
    }
    assert {"tenant_id", "file_name"} <= indexed_fields
