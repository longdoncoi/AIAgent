"""Multi-Retrieval Fusion combining Vector, lexical keyword, and Knowledge Graph streams."""

import logging
from collections import defaultdict
from typing import Any

from llama_index.core.schema import NodeWithScore

from src.agentic.core.config import get_settings

logger = logging.getLogger(__name__)


class MultiRetrievalFusion:
    """Combines Dense Vector, keyword, and Knowledge Graph lists using weighted RRF.

    Default channel weights come from ``Settings`` (``rag_vector_weight``,
    ``rag_lexical_weight``, ``rag_kg_weight``); no hardcoded production weights.
    """

    def __init__(self, rrf_k: int = 60) -> None:
        self.rrf_k = rrf_k
        settings = get_settings()
        self.channel_weights: dict[str, float] = {
            "vector": settings.rag_vector_weight,
            "keyword": settings.rag_lexical_weight,
            # Historical channel name kept as an alias for backward compatibility.
            "bm25": settings.rag_lexical_weight,
            "kg": settings.rag_kg_weight,
        }

    def fuse(
        self,
        retrieval_channels: dict[str, list[NodeWithScore]],
        channel_weights: dict[str, float] | None = None,
        top_k: int = 5,
    ) -> list[NodeWithScore]:
        """Fuses multiple retrieval channels using Weighted Reciprocal Rank Fusion.

        channels:
            {"vector": [...], "keyword": [...], "kg": [...]}
        """
        weights = channel_weights or self.channel_weights

        fused_scores: dict[str, float] = defaultdict(float)
        node_map: dict[str, Any] = {}

        for channel_name, rank_list in retrieval_channels.items():
            weight = weights.get(channel_name, 1.0)
            for rank, item in enumerate(rank_list, start=1):
                node_text = (
                    item.node.get_content()
                    if hasattr(item.node, "get_content")
                    else getattr(item.node, "text", "")
                )
                node_id = getattr(item.node, "node_id", node_text[:60])
                rrf_increment = weight / (self.rrf_k + rank)
                fused_scores[node_id] += rrf_increment

                if node_id not in node_map:
                    node_map[node_id] = item.node

        fused_items: list[NodeWithScore] = [
            NodeWithScore(node=node_map[nid], score=score) for nid, score in fused_scores.items()
        ]
        fused_items.sort(key=lambda x: x.score or 0.0, reverse=True)
        return fused_items[:top_k]


_fusion_instance: MultiRetrievalFusion | None = None


def get_fusion_engine() -> MultiRetrievalFusion:
    """Singleton getter for MultiRetrievalFusion."""
    global _fusion_instance
    if _fusion_instance is None:
        _fusion_instance = MultiRetrievalFusion()
    return _fusion_instance
