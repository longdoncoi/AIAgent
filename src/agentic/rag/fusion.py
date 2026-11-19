"""Multi-Retrieval Fusion combining Vector, BM25 Lexical, and Knowledge Graph streams."""

import logging
from collections import defaultdict
from typing import Any

from llama_index.core.schema import NodeWithScore

logger = logging.getLogger(__name__)


class MultiRetrievalFusion:
    """Combines ranked lists from Dense Vector, Lexical BM25, and Knowledge Graph using RRF."""

    def __init__(self, rrf_k: int = 60) -> None:
        self.rrf_k = rrf_k

    def fuse(
        self,
        retrieval_channels: dict[str, list[NodeWithScore]],
        channel_weights: dict[str, float] | None = None,
        top_k: int = 5,
    ) -> list[NodeWithScore]:
        """Fuses multiple retrieval channels using Weighted Reciprocal Rank Fusion.

        channels:
            {"vector": [...], "bm25": [...], "kg": [...]}
        """
        weights = channel_weights or {
            "vector": 0.40,
            "bm25": 0.30,
            "kg": 0.30,
        }

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
            NodeWithScore(node=node_map[nid], score=score)
            for nid, score in fused_scores.items()
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
