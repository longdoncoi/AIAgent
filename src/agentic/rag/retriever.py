"""Hybrid Retriever combining keyword scoring, dense embeddings, and auto-merging.

The lexical channel is a *keyword frequency heuristic* (``keyword_score``), not
true BM25: no IDF corpus statistics are computed and ``rank_bm25`` is not
installed. The name is kept honest in code and metrics.
"""

import logging
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any, cast

from llama_index.core import StorageContext, load_index_from_storage
from llama_index.core.schema import NodeWithScore, TextNode
from llama_index.core.vector_stores import FilterOperator, MetadataFilter, MetadataFilters

from src.agentic.core.config import get_settings
from src.agentic.rag.embedding import configure_embedding_model, invalidate_embedding_provider_cache
from src.agentic.rag.ingestion import load_documents_from_dir

logger = logging.getLogger(__name__)


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
        self._load_storage()

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
                logger.warning(
                    f"Could not load index from Qdrant ({e}). Falling back to local storage."
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
                logger.warning(f"Could not load index from {self.index_dir}: {e}")

    def keyword_score(self, query: str, top_k: int = 5) -> list[NodeWithScore]:
        """Lexical keyword-frequency scoring over the docstore (or raw files).

        This is NOT BM25: it has no IDF weighting, no length normalization and
        no ``rank_bm25`` dependency. It is a cheap heuristic channel that
        complements dense retrieval; fusion weights are configured via
        ``settings.rag_lexical_weight``.
        """
        terms = [t.lower() for t in query.split() if len(t) > 2]
        scored_nodes: list[NodeWithScore] = []

        if self._docstore:
            for node in self._docstore.docs.values():
                if isinstance(node, TextNode):
                    text_lower = node.text.lower()
                    score = sum(text_lower.count(term) for term in terms)
                    if score > 0:
                        scored_nodes.append(NodeWithScore(node=node, score=float(score)))
        else:
            for doc in load_documents_from_dir(self.raw_dir):
                text_lower = doc.text.lower()
                score = sum(text_lower.count(term) for term in terms)
                if score > 0:
                    node = TextNode(text=doc.text, metadata=doc.metadata)
                    scored_nodes.append(NodeWithScore(node=node, score=float(score)))

        scored_nodes.sort(key=lambda x: x.score or 0.0, reverse=True)
        return scored_nodes[:top_k]

    def _keyword_search(self, query: str, top_k: int = 5) -> list[NodeWithScore]:
        """Backward-compatible alias for :meth:`keyword_score` (not BM25)."""
        return self.keyword_score(query, top_k=top_k)

    def _build_metadata_filters(self, filters: dict[str, Any]) -> MetadataFilters | None:
        """Translate metadata filters into Qdrant/LlamaIndex pushdown filters.

        Tenant filters are pushed down in production (fail-closed); in
        development, untagged legacy documents stay visible via the post-filter.
        """
        filter_list: list[MetadataFilter | MetadataFilters] = []
        for key, expected in filters.items():
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
        return MetadataFilters(filters=filter_list) if filter_list else None

    def _is_permitted(self, item: NodeWithScore, filters: dict[str, Any]) -> bool:
        """Fail-closed metadata check: documents without a tenant tag are dropped in production."""
        metadata = getattr(item.node, "metadata", {}) or {}
        for key, expected in filters.items():
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
        self, query: str, top_k: int = 5, filters: dict[str, Any] | None = None
    ) -> list[NodeWithScore]:
        """Retrieves top_k nodes via AutoMergingRetriever over hierarchical leaf/parent nodes.

        Metadata filters are pushed into the vector-store query where supported
        (Qdrant), so tenant isolation is enforced at the storage boundary too.
        """
        if not self._index:
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
            logger.debug(
                f"AutoMergingRetriever not applicable ({e}). Using standard vector retriever."
            )
            try:
                retriever = self._index.as_retriever(similarity_top_k=top_k, **extra_kwargs)
                return cast(list[NodeWithScore], retriever.retrieve(query))
            except Exception:
                return []

    def _reciprocal_rank_fusion(
        self,
        retrieval_lists: list[list[NodeWithScore]],
        k: int = 60,
    ) -> list[NodeWithScore]:
        """Merges multiple ranked lists using Reciprocal Rank Fusion (RRF)."""
        rrf_scores: dict[str, float] = defaultdict(float)
        node_map: dict[str, Any] = {}

        for rank_list in retrieval_lists:
            for rank, item in enumerate(rank_list, start=1):
                node_text = (
                    item.node.get_content()
                    if hasattr(item.node, "get_content")
                    else getattr(item.node, "text", "")
                )
                node_id = getattr(item.node, "node_id", node_text[:50])
                rrf_scores[node_id] += 1.0 / (k + rank)
                if node_id not in node_map:
                    node_map[node_id] = item.node

        fused_items: list[NodeWithScore] = [
            NodeWithScore(node=node_map[nid], score=score) for nid, score in rrf_scores.items()
        ]
        fused_items.sort(key=lambda x: x.score or 0.0, reverse=True)
        return fused_items

    def retrieve(
        self, query: str, top_k: int = 5, filters: dict[str, Any] | None = None
    ) -> list[NodeWithScore]:
        """Main hybrid retrieval method combining Vector, keyword, and KG channels with RRF.

        When ``filters`` are supplied, every channel is post-filtered as well
        (defense in depth), and missing tenant tags are excluded in production.
        """
        effective_filters = filters or {}
        keyword_results = self.keyword_score(query, top_k=top_k * 2)
        dense_results = self._dense_search(query, top_k=top_k * 2, filters=effective_filters)

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
                kg_results = kg_store.query_graph(query, top_k=top_k)
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
        return fused[:top_k]

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

        return QdrantVectorStore(client=q_client, collection_name=target_collection)
    except Exception as e:
        logger.debug(f"QdrantVectorStore not available ({e}). Operating in local disk/memory mode.")
        return None
