"""Observability subsystem: Distributed Tracing, LangSmith, and Prometheus metrics."""

from src.agentic.observability.langsmith_tracer import (
    get_langsmith_callback,
    get_langsmith_client,
    is_langsmith_enabled,
    log_feedback,
    setup_langsmith,
    sync_evaluation_dataset,
    trace_langsmith_run,
    trace_langsmith_run_async,
    traceable_operation,
)
from src.agentic.observability.metrics import (
    A2A_REQUESTS_TOTAL,
    HITL_APPROVALS_TOTAL,
    NODE_DURATION,
    PROMPT_CACHE_HITS_TOTAL,
    PROMPT_CACHE_MISSES_TOTAL,
    RAG_CACHE_HITS_TOTAL,
    RAG_CACHE_MISSES_TOTAL,
    REQUEST_DURATION,
    TOKENS_TOTAL,
    TOOL_EXECUTION_TOTAL,
)
from src.agentic.observability.tracing import (
    extract_trace_context,
    get_tracer,
    inject_trace_context,
    trace_span,
    traced_operation,
)

__all__ = [
    "get_tracer",
    "trace_span",
    "traced_operation",
    "inject_trace_context",
    "extract_trace_context",
    "setup_langsmith",
    "is_langsmith_enabled",
    "get_langsmith_client",
    "get_langsmith_callback",
    "traceable_operation",
    "trace_langsmith_run",
    "trace_langsmith_run_async",
    "log_feedback",
    "sync_evaluation_dataset",
    "REQUEST_DURATION",
    "NODE_DURATION",
    "TOKENS_TOTAL",
    "HITL_APPROVALS_TOTAL",
    "A2A_REQUESTS_TOTAL",
    "TOOL_EXECUTION_TOTAL",
    "RAG_CACHE_HITS_TOTAL",
    "RAG_CACHE_MISSES_TOTAL",
    "PROMPT_CACHE_HITS_TOTAL",
    "PROMPT_CACHE_MISSES_TOTAL",
]
