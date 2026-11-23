"""Cross-Encoder reranker to refine relevance scores of retrieved chunks."""

import logging
import math

from llama_index.core.schema import NodeWithScore

logger = logging.getLogger(__name__)


class CrossEncoderReranker:
    """Reranker that re-evaluates the semantic matching of candidate nodes.

    Uses a neural Cross-Encoder model when available, with a normalized
    BM25/TF-IDF positional-weighted ranking fallback.
    """

    def __init__(self, model_name: str = "BAAI/bge-reranker-base", top_n: int = 3) -> None:
        self.model_name = model_name
        self.top_n = top_n
        self._cross_encoder = None
        self._init_model()

    def _init_model(self) -> None:
        """Attempts to load a neural CrossEncoder model if dependencies are satisfied."""
        try:
            from sentence_transformers import CrossEncoder

            self._cross_encoder = CrossEncoder(self.model_name)
            logger.info(f"Loaded neural CrossEncoder model: {self.model_name}")
        except Exception:
            logger.debug("Neural CrossEncoder not available. Using statistical relevance ranker.")

    def _fallback_score(self, query: str, text: str, base_score: float) -> float:
        """Statistical BM25-style term frequency + position-weighted scoring."""
        query_terms = [t.lower() for t in query.split() if len(t) > 2]
        if not query_terms:
            return base_score

        text_lower = text.lower()
        doc_words = text_lower.split()
        doc_len = max(1, len(doc_words))

        # BM25 parameters
        k1 = 1.5
        b = 0.75
        avg_doc_len = 120.0
        norm_len = (1.0 - b) + b * (doc_len / avg_doc_len)

        score = 0.0
        for term in query_terms:
            tf = doc_words.count(term)
            if tf > 0:
                tf_norm = (tf * (k1 + 1.0)) / (tf + k1 * norm_len)
                # Boost if term occurs early in text
                first_pos = text_lower.find(term)
                pos_boost = (
                    1.0 + (0.2 * max(0.0, 1.0 - (first_pos / 500.0))) if first_pos != -1 else 1.0
                )
                score += tf_norm * pos_boost

        # Combine with dense base_score and squash via sigmoid into [0, 1]
        raw_combined = (base_score * 0.5) + (score * 0.5)
        sigmoid_score = 1.0 / (1.0 + math.exp(-max(-10.0, min(10.0, raw_combined))))
        return round(sigmoid_score, 4)

    def rerank(self, query: str, nodes: list[NodeWithScore]) -> list[NodeWithScore]:
        """Reranks candidate nodes and returns the top_n highest scoring items."""
        if not nodes:
            return []

        # If neural cross-encoder is active
        if self._cross_encoder:
            try:
                pairs = [
                    [
                        query,
                        item.node.get_content()
                        if hasattr(item.node, "get_content")
                        else getattr(item.node, "text", ""),
                    ]
                    for item in nodes
                ]
                scores = self._cross_encoder.predict(pairs)
                for item, score in zip(nodes, scores, strict=False):
                    item.score = float(score)
                nodes.sort(key=lambda x: x.score or 0.0, reverse=True)
                return nodes[: self.top_n]
            except Exception as e:
                logger.warning(
                    f"Neural CrossEncoder inference failed ({e}). Falling back to statistical scoring."
                )

        # Fallback scoring
        scored: list[tuple[NodeWithScore, float]] = []
        for item in nodes:
            base = item.score or 0.0
            node_text = (
                item.node.get_content()
                if hasattr(item.node, "get_content")
                else getattr(item.node, "text", "")
            )
            computed = self._fallback_score(query, node_text, base)
            scored.append((item, computed))

        scored.sort(key=lambda x: x[1], reverse=True)

        reranked_nodes: list[NodeWithScore] = []
        for item, final_score in scored[: self.top_n]:
            item.score = final_score
            reranked_nodes.append(item)

        return reranked_nodes
