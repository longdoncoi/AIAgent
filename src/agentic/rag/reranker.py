"""Cross-Encoder reranker to refine relevance scores of retrieved chunks.

The primary path is a locally loaded neural cross-encoder (``sentence-
transformers``). It is loaded lazily through a lock-protected process-wide
cache: RAG tool cache misses must not reload a ~400MB model each time.

FALLBACK (heuristic): when ``sentence_transformers`` or the model weights are
unavailable, scoring degrades to a *statistical heuristic* (term frequency with
BM25-style saturation + positional boost blended with the dense score). This is
NOT a neural cross-encoder and its scores are only roughly calibrated. The
fallback is logged at WARNING and exported through
``agent_rag_reranker_mode{mode="heuristic"}`` so production can alert on it.
"""

import logging
import math
import threading
from typing import Any

from llama_index.core.schema import NodeWithScore

from src.agentic.observability.metrics import RAG_RERANKER_MODE

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "BAAI/bge-reranker-base"
_model_lock = threading.Lock()
_model_cache: dict[str, Any] = {}
_heuristic_warned = False


def _set_reranker_mode(mode: str) -> None:
    """Publish the selected mode once (shared metric, info-style counter)."""
    RAG_RERANKER_MODE.labels(mode=mode).inc()


def _get_cross_encoder(model_name: str) -> Any | None:
    """Return the shared lazily-initialized cross-encoder, or None if unavailable."""
    global _heuristic_warned
    with _model_lock:
        if model_name in _model_cache:
            return _model_cache[model_name]

        try:
            from sentence_transformers import CrossEncoder

            model: Any = CrossEncoder(model_name)
            _model_cache[model_name] = model
            _set_reranker_mode("neural")
            logger.info("Loaded neural CrossEncoder model: %s", model_name)
        except Exception as exc:
            # Heuristic fallback -- intentionally WARNING, not debug: silently
            # using it would make relevance scores look trustworthy when they are not.
            _model_cache[model_name] = None
            _set_reranker_mode("heuristic")
            if not _heuristic_warned:
                logger.warning(
                    "Neural cross-encoder '%s' unavailable (%s); using HEURISTIC reranking. "
                    "Relevance scores are approximate; install sentence_transformers and "
                    "the model weights for production ranking quality.",
                    model_name,
                    exc,
                )
                _heuristic_warned = True
        return _model_cache[model_name]


class CrossEncoderReranker:
    """Reranks candidates with a shared neural cross-encoder, heuristic as fallback."""

    def __init__(self, model_name: str | None = DEFAULT_MODEL, top_n: int = 3) -> None:
        self.model_name = model_name or DEFAULT_MODEL
        self.top_n = top_n

    @property
    def _cross_encoder(self) -> Any | None:
        """Lazily resolve the process-wide model (also keeps legacy callers working)."""
        return _get_cross_encoder(self.model_name)

    @_cross_encoder.setter
    def _cross_encoder(self, model: Any | None) -> None:
        """Inject a model into the shared cache (used by tests and warm-up hooks)."""
        with _model_lock:
            _model_cache[self.model_name] = model

    def _fallback_score(self, query: str, text: str, base_score: float) -> float:
        """Heuristic TF + position-weighted scoring (BM25-style saturation, no IDF)."""
        query_terms = [t.lower() for t in query.split() if len(t) > 2]
        if not query_terms:
            return base_score

        text_lower = text.lower()
        doc_words = text_lower.split()
        doc_len = max(1, len(doc_words))

        # BM25-style saturation parameters (no corpus IDF -- this is a heuristic).
        k1 = 1.5
        b = 0.75
        avg_doc_len = 120.0
        norm_len = (1.0 - b) + b * (doc_len / avg_doc_len)

        score = 0.0
        for term in query_terms:
            tf = doc_words.count(term)
            if tf > 0:
                tf_norm = (tf * (k1 + 1.0)) / (tf + k1 * norm_len)
                first_pos = text_lower.find(term)
                pos_boost = (
                    1.0 + (0.2 * max(0.0, 1.0 - (first_pos / 500.0))) if first_pos != -1 else 1.0
                )
                score += tf_norm * pos_boost

        raw_combined = (base_score * 0.5) + (score * 0.5)
        sigmoid_score = 1.0 / (1.0 + math.exp(-max(-10.0, min(10.0, raw_combined))))
        return round(sigmoid_score, 4)

    def rerank(self, query: str, nodes: list[NodeWithScore]) -> list[NodeWithScore]:
        """Reranks candidate nodes and returns the top_n highest scoring items."""
        if not nodes:
            return []

        encoder = self._cross_encoder
        if encoder is not None:
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
                scores = encoder.predict(pairs)
                for item, score in zip(nodes, scores, strict=False):
                    item.score = float(score)
                nodes.sort(key=lambda x: x.score or 0.0, reverse=True)
                return nodes[: self.top_n]
            except Exception as exc:
                logger.warning(
                    "Neural cross-encoder inference failed (%s). Falling back to heuristic.",
                    exc,
                )

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
