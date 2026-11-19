"""Hybrid Retriever combining BM25 keyword search, dense embeddings, and auto-merging."""

import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

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
        """Attempts to load persisted index from disk."""
        if self.index_dir.exists() and (self.index_dir / "docstore.json").exists():
            try:
                import socket

                from llama_index.core import Settings as LlamaSettings

                has_embed_server = False
                try:
                    with socket.create_connection(("localhost", 8081), timeout=0.2):
                        has_embed_server = True
                except Exception:
                    pass

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

                storage_context = StorageContext.from_defaults(persist_dir=str(self.index_dir))
                self._storage_context = storage_context
                self._index = load_index_from_storage(storage_context)
                self._docstore = storage_context.docstore
                logger.info("Successfully loaded VectorStoreIndex and StorageContext from persist directory.")
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
                retriever = AutoMergingRetriever(base_retriever, storage_context=self._storage_context, verbose=False)
                return retriever.retrieve(query)
            retriever = self._index.as_retriever(similarity_top_k=top_k)
            return retriever.retrieve(query)
        except Exception as e:
            logger.debug(f"AutoMergingRetriever not applicable ({e}). Using standard vector retriever.")
            try:
                retriever = self._index.as_retriever(similarity_top_k=top_k)
                return retriever.retrieve(query)
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
                    item.node.get_content() if hasattr(item.node, "get_content") else getattr(item.node, "text", "")
                )
                node_id = getattr(item.node, "node_id", node_text[:50])
                rrf_scores[node_id] += 1.0 / (k + rank)
                if node_id not in node_map:
                    node_map[node_id] = item.node

        fused_items: list[NodeWithScore] = [
            NodeWithScore(node=node_map[nid], score=score)
            for nid, score in rrf_scores.items()
        ]
        fused_items.sort(key=lambda x: x.score or 0.0, reverse=True)
        return fused_items

    def retrieve(self, query: str, top_k: int = 5) -> list[NodeWithScore]:
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
        return fusion.fuse(channels, top_k=top_k)


_retriever_instance: HybridRetriever | None = None


def get_hybrid_retriever() -> HybridRetriever:
    """Returns singleton HybridRetriever instance."""
    global _retriever_instance
    if _retriever_instance is None:
        _retriever_instance = HybridRetriever()
    return _retriever_instance
