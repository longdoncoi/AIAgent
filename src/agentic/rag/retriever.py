"""Hybrid Retriever combining BM25, dense embeddings, and auto-merging."""

import logging
import re
import threading
from pathlib import Path
from typing import Any, cast

from llama_index.core import StorageContext, load_index_from_storage
from llama_index.core.schema import NodeWithScore, TextNode
from llama_index.core.vector_stores import (
    FilterCondition,
    FilterOperator,
    MetadataFilter,
    MetadataFilters,
)

from src.agentic.core.config import get_settings
from src.agentic.rag.embedding import configure_embedding_model, invalidate_embedding_provider_cache
from src.agentic.rag.ingestion import load_documents_from_dir
from src.agentic.rag.metrics import (
    RAG_DENSE_RETRIEVAL_ERRORS_TOTAL,
    RAG_RETRIEVAL_DEGRADED_TOTAL,
)

logger = logging.getLogger(__name__)


class RetrievalResult(list[NodeWithScore]):
    """A list of nodes that also carries a fail-closed degradation signal.

    It remains a plain ``list`` subclass so all existing callers behave
    identically, while callers that care about quality can check
    ``result.degraded`` instead of mistaking an empty/fallback result for a
    healthy one.
    """

    def __init__(
        self,
        nodes: list[NodeWithScore] | None = None,
        *,
        degraded: bool = False,
        degraded_reasons: list[str] | None = None,
    ) -> None:
        super().__init__(nodes or [])
        self.degraded = degraded
        self.degraded_reasons: list[str] = degraded_reasons or []


class HybridRetriever:
    """Performs hybrid retrieval (keyword + dense embedding) with Reciprocal Rank Fusion."""

    def __init__(self, index_dir: Path | None = None) -> None:
        self.settings = get_settings()
        self.index_dir = index_dir or (self.settings.project_root / "data" / "index")
        self.raw_dir = self.settings.project_root / "data" / "raw"
        self._index: Any = None
        self._docstore: Any = None
        self._storage_context: StorageContext | None = None
        self._invalidate_lock = threading.Lock()
        self._degraded_reasons: list[str] = []
        self._load_storage()

    @property
    def degraded(self) -> bool:
        """True when any retrieval backend failed to load or respond."""
        return bool(self._degraded_reasons)

    def _mark_degraded(self, reason: str, *, metric: Any = None) -> None:
        """Record a degradation signal (never a silent fallback)."""
        if reason not in self._degraded_reasons:
            self._degraded_reasons.append(reason)
        if metric is not None:
            metric.inc()

    def _load_storage(self) -> None:
        """Attempts to load persisted index from Qdrant vector store or disk.

        Embedding configuration is delegated to the shared, fail-closed resolver;
        a required-but-unreachable embedding service raises RuntimeError here.
        """
        configure_embedding_model()

        # 1. Try Qdrant if enabled
        qdrant_store = get_qdrant_vector_store()
        if qdrant_store is not None:
            try:
                from llama_index.core import VectorStoreIndex

                if self.index_dir.exists() and (self.index_dir / "docstore.json").exists():
                    storage_context = StorageContext.from_defaults(
                        vector_store=qdrant_store,
                        persist_dir=str(self.index_dir),
                    )
                else:
                    storage_context = StorageContext.from_defaults(vector_store=qdrant_store)

                self._storage_context = storage_context
                self._index = VectorStoreIndex.from_vector_store(
                    qdrant_store, storage_context=storage_context
                )
                self._docstore = storage_context.docstore
                logger.info(
                    "Successfully loaded VectorStoreIndex and StorageContext backed by Qdrant (HNSW mmap + Scalar Quantization)."
                )
                return
            except Exception as e:
                # ERROR (not debug/warning): a broken Qdrant load means the
                # service may serve stale local data without anyone noticing.
                logger.error(
                    "Could not load index from Qdrant (%s). Falling back to local storage; "
                    "results may be STALE.",
                    e,
                )
                self._mark_degraded(
                    "qdrant_load_failed",
                    metric=RAG_DENSE_RETRIEVAL_ERRORS_TOTAL.labels(reason="qdrant_load_failed"),
                )

        # 2. Local disk fallback
        if self.index_dir.exists() and (self.index_dir / "docstore.json").exists():
            try:
                storage_context = StorageContext.from_defaults(persist_dir=str(self.index_dir))
                self._storage_context = storage_context
                self._index = load_index_from_storage(storage_context)
                self._docstore = storage_context.docstore
                logger.info(
                    "Successfully loaded VectorStoreIndex and StorageContext from persist directory."
                )
            except Exception as e:
                logger.error("Could not load index from %s: %s", self.index_dir, e)
                self._mark_degraded(
                    "local_index_load_failed",
                    metric=RAG_DENSE_RETRIEVAL_ERRORS_TOTAL.labels(
                        reason="local_index_load_failed"
                    ),
                )
        elif qdrant_store is None:
            self._mark_degraded(
                "no_vector_store",
                metric=RAG_DENSE_RETRIEVAL_ERRORS_TOTAL.labels(reason="no_vector_store"),
            )

    def keyword_score(self, query: str, top_k: int = 5) -> list[NodeWithScore]:
        """Rank the corpus with BM25Okapi; production fails closed if unavailable."""
        query_terms = [term for term in re.findall(r"\w+", query.lower()) if len(term) > 2]
        if not query_terms:
            return []
        nodes: list[TextNode] = []
        if self._docstore:
            for node in self._docstore.docs.values():
                if isinstance(node, TextNode):
                    nodes.append(node)
        else:
            for doc in load_documents_from_dir(self.raw_dir):
                nodes.append(TextNode(text=doc.text, metadata=doc.metadata))
        if not nodes:
            return []

        tokenized = [re.findall(r"\w+", node.text.lower()) for node in nodes]
        try:
            from rank_bm25 import BM25Okapi

            scores = BM25Okapi(tokenized).get_scores(query_terms)
        except ImportError as exc:
            if self.settings.require_neural_reranker:
                raise RuntimeError("rank-bm25 is required in production") from exc
            # Development-only compatibility path for lightweight installs.
            scores = [
                float(sum(tokens.count(term) for term in query_terms))
                for tokens in tokenized
            ]

        scored_nodes = [
            NodeWithScore(node=node, score=float(score))
            for node, score in zip(nodes, scores, strict=True)
            if float(score) > 0
        ]

        scored_nodes.sort(key=lambda x: x.score or 0.0, reverse=True)
        return scored_nodes[:top_k]

    def _keyword_search(self, query: str, top_k: int = 5) -> list[NodeWithScore]:
        """Backward-compatible alias for :meth:`keyword_score`."""
        return self.keyword_score(query, top_k=top_k)

    def _build_metadata_filters(self, filters: dict[str, Any]) -> MetadataFilters | None:
        """Translate metadata filters into Qdrant/LlamaIndex pushdown filters.

        Tenant filters are pushed down in production (fail-closed); in
        development, untagged legacy documents stay visible via the post-filter.
        """
        filter_list: list[MetadataFilter | MetadataFilters] = []
        for key, expected in filters.items():
            if key == "user_roles":
                values = sorted({str(value) for value in expected} | {"*"})
                filter_list.append(
                    MetadataFilter(
                        key="allowed_roles", value=values, operator=FilterOperator.ANY
                    )
                )
                continue
            if key == "department":
                filter_list.append(
                    MetadataFilter(
                        key="department",
                        value=[str(expected), "*"],
                        operator=FilterOperator.IN,
                    )
                )
                continue
            if key == "acl_principals":
                values = sorted({str(value) for value in expected} | {"public"})
                filter_list.append(
                    MetadataFilter(key="acl", value=values, operator=FilterOperator.ANY)
                )
                continue
            if key == "tenant_id" and not self.settings.require_embedding_service:
                continue
            if isinstance(expected, (list, tuple, set)):
                filter_list.append(
                    MetadataFilter(key=key, value=list(expected), operator=FilterOperator.IN)
                )
            else:
                filter_list.append(
                    MetadataFilter(key=key, value=expected, operator=FilterOperator.EQ)
                )
        return (
            MetadataFilters(filters=filter_list, condition=FilterCondition.AND)
            if filter_list
            else None
        )

    def _is_permitted(self, item: NodeWithScore, filters: dict[str, Any]) -> bool:
        """Fail-closed metadata check: documents without a tenant tag are dropped in production."""
        metadata = getattr(item.node, "metadata", {}) or {}
        for key, expected in filters.items():
            if key == "user_roles":
                allowed = metadata.get("allowed_roles")
                if not isinstance(allowed, (list, tuple, set)):
                    return False if self.settings.require_embedding_service else True
                if "*" not in allowed and not set(map(str, expected)).intersection(map(str, allowed)):
                    return False
                continue
            if key == "department":
                actual_department = metadata.get("department")
                if actual_department is None:
                    return False if self.settings.require_embedding_service else True
                if str(actual_department) not in {"*", str(expected)}:
                    return False
                continue
            if key == "acl_principals":
                acl = metadata.get("acl")
                if not isinstance(acl, (list, tuple, set)):
                    return False if self.settings.require_embedding_service else True
                if not set(map(str, expected)).intersection(map(str, acl)):
                    return False
                continue
            actual = metadata.get(key)
            if key == "tenant_id" and actual is None:
                # Legacy/shared documents must not be visible to any tenant in production.
                if self.settings.require_embedding_service:
                    return False
                continue
            if isinstance(expected, (list, tuple, set)):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True

    def _dense_search(
        self,
        query: str,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
        degraded_reasons: list[str] | None = None,
    ) -> list[NodeWithScore]:
        """Retrieves top_k nodes via AutoMergingRetriever over hierarchical leaf/parent nodes.

        Metadata filters are pushed into the vector-store query where supported
        (Qdrant), so tenant isolation is enforced at the storage boundary too.
        Failures are logged at ERROR, counted, and recorded in ``degraded_reasons``
        instead of being silently swallowed.
        """
        if not self._index:
            if degraded_reasons is not None:
                degraded_reasons.append("dense_index_unavailable")
            return []
        pushdown = self._build_metadata_filters(filters or {})
        extra_kwargs: dict[str, Any] = {"filters": pushdown} if pushdown is not None else {}
        try:
            if self._storage_context:
                from llama_index.core.retrievers import AutoMergingRetriever

                base_retriever = self._index.as_retriever(
                    similarity_top_k=top_k, **extra_kwargs
                )
                retriever = AutoMergingRetriever(
                    base_retriever, storage_context=self._storage_context, verbose=False
                )
                return retriever.retrieve(query)
            retriever = self._index.as_retriever(similarity_top_k=top_k, **extra_kwargs)
            return cast(list[NodeWithScore], retriever.retrieve(query))
        except Exception as e:
            logger.warning("AutoMergingRetriever not applicable (%s). Using standard retriever.", e)
            try:
                retriever = self._index.as_retriever(similarity_top_k=top_k, **extra_kwargs)
                return cast(list[NodeWithScore], retriever.retrieve(query))
            except Exception as exc:
                logger.error("Dense retrieval channel failed: %s", exc)
                RAG_DENSE_RETRIEVAL_ERRORS_TOTAL.labels(reason="dense_query_failed").inc()
                self._mark_degraded(
                    "dense_query_failed",
                    metric=RAG_RETRIEVAL_DEGRADED_TOTAL.labels(reason="dense_query_failed"),
                )
                if degraded_reasons is not None:
                    degraded_reasons.append("dense_query_failed")
                return []

    def _reciprocal_rank_fusion(
        self,
        retrieval_lists: list[list[NodeWithScore]],
        k: int = 60,
    ) -> list[NodeWithScore]:
        """Backward-compatible RRF wrapper around the single fusion engine.

        The former duplicate implementation here drifted from the weighted
        fusion used by :meth:`retrieve`; delegate instead of maintaining two.
        """
        from src.agentic.rag.fusion import MultiRetrievalFusion

        channels = {f"channel_{idx}": items for idx, items in enumerate(retrieval_lists)}
        total = sum(len(items) for items in retrieval_lists)
        return MultiRetrievalFusion(rrf_k=k).fuse(channels, top_k=total)

    def retrieve(
        self, query: str, top_k: int = 5, filters: dict[str, Any] | None = None
    ) -> list[NodeWithScore]:
        """Main hybrid retrieval method combining Vector, keyword, and KG channels with RRF.

        When ``filters`` are supplied, every channel is post-filtered as well
        (defense in depth), and missing tenant tags are excluded in production.
        """
        effective_filters = filters or {}
        degraded_reasons: list[str] = list(self._degraded_reasons)
        keyword_results = self.keyword_score(query, top_k=top_k * 2)
        dense_results = self._dense_search(
            query, top_k=top_k * 2, filters=effective_filters, degraded_reasons=degraded_reasons
        )

        if effective_filters:
            keyword_results = [
                item for item in keyword_results if self._is_permitted(item, effective_filters)
            ]
            dense_results = [
                item for item in dense_results if self._is_permitted(item, effective_filters)
            ]

        # Knowledge Graph Retrieval
        kg_results: list[NodeWithScore] = []
        if self.settings.enable_knowledge_graph:
            try:
                from src.agentic.rag.kg import get_knowledge_graph_store

                kg_store = get_knowledge_graph_store()
                kg_results = kg_store.query_graph(
                    query, top_k=top_k, filters=effective_filters
                )
                if effective_filters:
                    kg_results = [
                        item for item in kg_results if self._is_permitted(item, effective_filters)
                    ]
            except Exception as e:
                logger.debug(f"Knowledge Graph retrieval skipped ({e})")

        from src.agentic.rag.fusion import get_fusion_engine

        channels: dict[str, list[NodeWithScore]] = {
            "keyword": keyword_results,
            "vector": dense_results,
        }
        if kg_results:
            channels["kg"] = kg_results

        fusion = get_fusion_engine()
        fused = fusion.fuse(channels, top_k=top_k * 2 if effective_filters else top_k)
        if effective_filters:
            fused = [item for item in fused if self._is_permitted(item, effective_filters)]
        return RetrievalResult(
            fused[:top_k],
            degraded=bool(degraded_reasons),
            degraded_reasons=degraded_reasons,
        )

    def invalidate(self) -> None:
        """Drop cached index/docstore/embedding state and reload fresh storage.

        Public API for callers that rebuild or atomically swap the index in
        another process -- notably ``workflows/rag_workflow.py``. Call this
        after ingestion so the next retrieval does not serve stale cross-process
        index state; it also resets the shared embedding-provider cache.
        """
        with self._invalidate_lock:
            self._index = None
            self._docstore = None
            self._storage_context = None
            self._degraded_reasons = []
            invalidate_embedding_provider_cache()
            self._load_storage()


_retriever_instance: HybridRetriever | None = None


def get_hybrid_retriever() -> HybridRetriever:
    """Returns singleton HybridRetriever instance."""
    global _retriever_instance
    if _retriever_instance is None:
        _retriever_instance = HybridRetriever()
    return _retriever_instance


def reset_hybrid_retriever(
    doc_id: str = "", file_path: str = "", generation: int = 0
) -> None:
    """Discard stale in-memory index state after an atomic ingestion rebuild.

    The optional arguments let this function be registered directly as a
    ``RAGCache`` invalidation callback ``(doc_id, file_path, generation)`` so
    cross-replica invalidations rebuild this pod's process-local retriever.
    Prefer :meth:`HybridRetriever.invalidate` on a live instance when available;
    this also resets the embedding-provider cache so config reloads are honoured.
    """
    global _retriever_instance
    logger.info(
        "Resetting hybrid retriever singleton (doc_id=%s, file_path=%s, generation=%s).",
        doc_id,
        file_path,
        generation,
    )
    _retriever_instance = None
    invalidate_embedding_provider_cache()


def _register_invalidation_callback() -> None:
    """Wire cross-replica RAG cache invalidations to this process-local retriever."""
    try:
        from src.agentic.cache.rag_cache import get_rag_cache

        get_rag_cache().register_invalidation_callback(reset_hybrid_retriever)
    except Exception as exc:  # pragma: no cover - best-effort wiring
        logger.warning("Could not register RAG invalidation callback: %s", exc)


_register_invalidation_callback()


def get_qdrant_vector_store(
    collection_name: str | None = None,
    embed_dim: int | None = None,
    client: Any | None = None,
) -> Any | None:
    """Initializes QdrantVectorStore with HNSW on-disk mmap and INT8 Scalar Quantization.

    Optimizations for Production:
    - HNSW on_disk=True: Maps graph links into disk memory map, reducing RAM consumption by up to 70%.
    - Scalar Quantization (INT8): Quantizes 32-bit float vectors to 8-bit integers, reducing memory by 4x.
    """
    settings = get_settings()
    target_dim = embed_dim or settings.default_embedding_dim
    if not settings.enable_qdrant and client is None:
        return None

    replication_factor = int(settings.qdrant_replication_factor)
    write_consistency = int(settings.qdrant_write_consistency_factor)
    if write_consistency > replication_factor:
        raise ValueError(
            "qdrant_write_consistency_factor must be <= qdrant_replication_factor "
            f"(got {write_consistency} > {replication_factor})."
        )

    target_collection = collection_name or settings.qdrant_collection_name
    try:
        from llama_index.vector_stores.qdrant import QdrantVectorStore
        from qdrant_client import QdrantClient
        from qdrant_client.http import models as qmodels

        q_client = client or QdrantClient(
            host=settings.qdrant_host,
            port=settings.qdrant_port,
            api_key=settings.qdrant_api_key,
            timeout=5,
        )

        collections = [c.name for c in q_client.get_collections().collections]

        if target_collection not in collections:
            create_kwargs: dict[str, Any] = {
                "collection_name": target_collection,
                "vectors_config": qmodels.VectorParams(
                    size=target_dim,
                    distance=qmodels.Distance.COSINE,
                    on_disk=True,
                ),
                "hnsw_config": qmodels.HnswConfigDiff(
                    m=16,
                    ef_construct=100,
                    on_disk=True,
                ),
                "quantization_config": qmodels.ScalarQuantization(
                    scalar=qmodels.ScalarQuantizationConfig(
                        type=qmodels.ScalarType.INT8,
                        quantile=0.99,
                        always_ram=True,
                    )
                ),
            }
            if settings.qdrant_shard_number > 1:
                create_kwargs["shard_number"] = settings.qdrant_shard_number
            if settings.qdrant_replication_factor > 1:
                create_kwargs["replication_factor"] = settings.qdrant_replication_factor
            if settings.qdrant_write_consistency_factor > 1:
                create_kwargs["write_consistency_factor"] = settings.qdrant_write_consistency_factor

            q_client.create_collection(**create_kwargs)
            logger.info(
                f"Configured Qdrant collection '{target_collection}' with HNSW on-disk mmap, INT8 Scalar Quantization, "
                f"shards={settings.qdrant_shard_number}, replication={settings.qdrant_replication_factor}."
            )

        # Payload indexes make tenant/file filters fast AND enforced at the
        # storage boundary. Safe to create repeatedly (Qdrant is idempotent).
        for field_name in ("tenant_id", "file_name", "department", "allowed_roles", "acl"):
            try:
                q_client.create_payload_index(
                    collection_name=target_collection,
                    field_name=field_name,
                    field_schema=qmodels.PayloadSchemaType.KEYWORD,
                    wait=True,
                )
            except Exception as exc:
                logger.debug(
                    "Could not create Qdrant payload index for %s: %s", field_name, exc
                )

        return QdrantVectorStore(client=q_client, collection_name=target_collection)
    except Exception as e:
        logger.debug(f"QdrantVectorStore not available ({e}). Operating in local disk/memory mode.")
        return None
