"""RAG-specific hardening metrics owned by the RAG subsystem.

These counters complement the shared metrics in
``src.agentic.observability.metrics`` (which this module must not edit). They
make silent degradation observable: a dense-channel failure, a reranker
fallback or an embedding-probe mismatch is always counted, and callers can
additionally read a ``degraded`` flag on retrieval results.
"""

from prometheus_client import Counter

RAG_DENSE_RETRIEVAL_ERRORS_TOTAL = Counter(
    "agent_rag_dense_retrieval_errors_total",
    "Dense retrieval channel failures (failures are no longer silent)",
    ["reason"],
)

RAG_RETRIEVAL_DEGRADED_TOTAL = Counter(
    "agent_rag_retrieval_degraded_total",
    "Retrieval requests that completed with one or more degraded channels",
    ["reason"],
)

RAG_RERANKER_FALLBACK_TOTAL = Counter(
    "agent_rag_reranker_fallback_total",
    "Cross-encoder load/inference failures that fell back to heuristic scoring",
    ["reason"],
)

RAG_EMBED_PROBE_ERRORS_TOTAL = Counter(
    "agent_rag_embed_probe_errors_total",
    "Embedding model probe failures or dimension mismatches",
    ["reason"],
)
