"""Hybrid Retriever combining BM25 keyword search, dense embeddings, and auto-merging."""

import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, cast

from llama_index.core import StorageContext, load_index_from_storage
from llama_index.core.schema import NodeWithScore, TextNode

from src.agentic.core.config import get_settings
from src.agentic.rag.ingestion import load_documents_from_dir

logger = logging.getLogger(__name__)


class HybridRetriever:
    """Performs hybrid retrieval (BM25 keyword + dense embedding) with Reciprocal Rank Fusion."""

    def __init__(self, index_dir: Path | None = None) -> None:
        self.settings = get_settings()
        self.index_dir = index_dir or (self.settings.project_root / "data" / "index")
        self.raw_dir = self.settings.project_root / "data" / "raw"
        self._index: Any = None
        self._docstore: Any = None
        self._storage_context: StorageContext | None = None
        self._load_storage()

    def _load_storage(self) -> None:
        """Attempts to load persisted index from Qdrant vector store or disk."""
        import socket

        from llama_index.core import Settings as LlamaSettings

        has_embed_server = False
        try:
            with socket.create_connection(("localhost", 8081), timeout=0.2):
                has_embed_server = True
        except Exception:
            pass

        try:
            if has_embed_server:
                from llama_index.embeddings.openai import OpenAIEmbedding

                LlamaSettings.embed_model = OpenAIEmbedding(
                    api_base=self.settings.llama_embed_base_url,
                    api_key="sk-dummy",
                    model_name=self.settings.default_embedding_model,
                )
            else:
                from llama_index.core.embeddings.mock_embed_model import MockEmbedding

                LlamaSettings.embed_model = MockEmbedding(embed_dim=768)
        except Exception as e:
            logger.debug(f"Could not configure embedding model: {e}")

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

    def _keyword_search(self, query: str, top_k: int = 5) -> list[NodeWithScore]:
        """Simple BM25 / lexical keyword matching fallback over local files or docstore."""
        terms = [t.lower() for t in query.split() if len(t) > 2]
        scored_nodes: list[NodeWithScore] = []

        # If docstore is loaded, search nodes in docstore
        if self._docstore:
            for node_id, node in self._docstore.docs.items():
                if isinstance(node, TextNode):
                    text_lower = node.text.lower()
                    score = sum(text_lower.count(term) for term in terms)
                    if score > 0:
                        scored_nodes.append(NodeWithScore(node=node, score=float(score)))
        else:
            # Fallback to searching data/raw files directly
            docs = load_documents_from_dir(self.raw_dir)
            for doc in docs:
                text_lower = doc.text.lower()
                score = sum(text_lower.count(term) for term in terms)
                if score > 0:
                    node = TextNode(text=doc.text, metadata=doc.metadata)
                    scored_nodes.append(NodeWithScore(node=node, score=float(score)))

        scored_nodes.sort(key=lambda x: x.score or 0.0, reverse=True)
        return scored_nodes[:top_k]

    def _dense_search(self, query: str, top_k: int = 5) -> list[NodeWithScore]:
        """Retrieves top_k nodes via AutoMergingRetriever over hierarchical leaf/parent nodes."""
        if not self._index:
            return []
        try:
            if self._storage_context:
                from llama_index.core.retrievers import AutoMergingRetriever

                base_retriever = self._index.as_retriever(similarity_top_k=top_k)
                retriever = AutoMergingRetriever(
                    base_retriever, storage_context=self._storage_context, verbose=False
                )
                return retriever.retrieve(query)
            retriever = self._index.as_retriever(similarity_top_k=top_k)
            return cast(list[NodeWithScore], retriever.retrieve(query))
        except Exception as e:
            logger.debug(
                f"AutoMergingRetriever not applicable ({e}). Using standard vector retriever."
            )
            try:
                retriever = self._index.as_retriever(similarity_top_k=top_k)
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
        """Main hybrid retrieval method combining Vector, Lexical BM25, and Knowledge Graph with RRF."""
        keyword_results = self._keyword_search(query, top_k=top_k * 2)
        dense_results = self._dense_search(query, top_k=top_k * 2)

        # Knowledge Graph Retrieval
        kg_results: list[NodeWithScore] = []
        if self.settings.enable_knowledge_graph:
            try:
                from src.agentic.rag.kg import get_knowledge_graph_store

                kg_store = get_knowledge_graph_store()
                kg_results = kg_store.query_graph(query, top_k=top_k)
            except Exception as e:
                logger.debug(f"Knowledge Graph retrieval skipped ({e})")

        from src.agentic.rag.fusion import get_fusion_engine

        channels: dict[str, list[NodeWithScore]] = {
            "bm25": keyword_results,
            "vector": dense_results,
        }
        if kg_results:
            channels["kg"] = kg_results

        fusion = get_fusion_engine()
        fused = fusion.fuse(channels, top_k=top_k * 2 if filters else top_k)
        if filters:
            def permitted(item: NodeWithScore) -> bool:
                metadata = getattr(item.node, "metadata", {}) or {}
                for key, expected in filters.items():
                    actual = metadata.get(key)
                    if key == "tenant_id" and actual is None:
                        # Legacy/shared documents remain visible; tenant-tagged documents do not leak.
                        continue
                    if isinstance(expected, (list, tuple, set)):
                        if actual not in expected:
                            return False
                    elif actual != expected:
                        return False
                return True

            fused = [item for item in fused if permitted(item)]
        return fused[:top_k]


_retriever_instance: HybridRetriever | None = None


def get_hybrid_retriever() -> HybridRetriever:
    """Returns singleton HybridRetriever instance."""
    global _retriever_instance
    if _retriever_instance is None:
        _retriever_instance = HybridRetriever()
    return _retriever_instance


def reset_hybrid_retriever() -> None:
    """Discard stale in-memory index state after an atomic ingestion rebuild."""
    global _retriever_instance
    _retriever_instance = None


def get_qdrant_vector_store(
    collection_name: str | None = None,
    embed_dim: int = 768,
    client: Any | None = None,
) -> Any | None:
    """Initializes QdrantVectorStore with HNSW on-disk mmap and INT8 Scalar Quantization.

    Optimizations for Production:
    - HNSW on_disk=True: Maps graph links into disk memory map, reducing RAM consumption by up to 70%.
    - Scalar Quantization (INT8): Quantizes 32-bit float vectors to 8-bit integers, reducing memory by 4x.
    """
    settings = get_settings()
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
            q_client.create_collection(
                collection_name=target_collection,
                vectors_config=qmodels.VectorParams(
                    size=embed_dim,
                    distance=qmodels.Distance.COSINE,
                    on_disk=True,
                ),
                hnsw_config=qmodels.HnswConfigDiff(
                    m=16,
                    ef_construct=100,
                    on_disk=True,
                ),
                quantization_config=qmodels.ScalarQuantization(
                    scalar=qmodels.ScalarQuantizationConfig(
                        type=qmodels.ScalarType.INT8,
                        quantile=0.99,
                        always_ram=True,
                    )
                ),
            )
            logger.info(
                f"Configured Qdrant collection '{target_collection}' with HNSW on-disk mmap and INT8 Scalar Quantization."
            )

        return QdrantVectorStore(client=q_client, collection_name=target_collection)
    except Exception as e:
        logger.debug(f"QdrantVectorStore not available ({e}). Operating in local disk/memory mode.")
        return None
