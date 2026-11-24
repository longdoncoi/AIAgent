"""Prometheus metrics for Agentic Platform monitoring."""

from prometheus_client import Counter, Gauge, Histogram

REQUEST_DURATION = Histogram(
    "agent_request_duration_seconds",
    "Time spent processing agent requests",
    ["component", "status"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)

TTFT_DURATION = Histogram(
    "agent_time_to_first_token_seconds",
    "Time from request start to first streamed token (TTFT)",
    ["endpoint"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

NODE_DURATION = Histogram(
    "agent_node_duration_seconds",
    "Time spent processing individual graph nodes",
    ["node_name"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

TOKENS_TOTAL = Counter(
    "agent_tokens_total",
    "Total tokens consumed by agent components",
    ["model_alias", "token_type"],
)

HITL_APPROVALS_TOTAL = Counter(
    "agent_hitl_approvals_total",
    "Human-in-the-loop approval actions",
    ["decision", "risk_level"],
)

HITL_PENDING_APPROVALS = Gauge(
    "agent_hitl_pending_approvals",
    "Number of executions currently paused waiting for human approval",
)

A2A_REQUESTS_TOTAL = Counter(
    "agent_a2a_requests_total",
    "Total A2A RPC requests dispatched",
    ["caller", "target_agent", "status"],
)

TOOL_EXECUTION_TOTAL = Counter(
    "agent_tool_executions_total",
    "Total MCP tool invocations",
    ["tool_name", "status"],
)

# Caching Metrics
RAG_CACHE_HITS_TOTAL = Counter(
    "agent_rag_cache_hits_total",
    "Total cache hits for RAG queries",
)

RAG_CACHE_MISSES_TOTAL = Counter(
    "agent_rag_cache_misses_total",
    "Total cache misses for RAG queries",
)

PROMPT_CACHE_HITS_TOTAL = Counter(
    "agent_prompt_cache_hits_total",
    "Total cache hits for Prompt completions",
    ["model_alias"],
)

PROMPT_CACHE_MISSES_TOTAL = Counter(
    "agent_prompt_cache_misses_total",
    "Total cache misses for Prompt completions",
    ["model_alias"],
)

LTM_WRITE_ERRORS_TOTAL = Counter(
    "agent_ltm_write_errors_total",
    "Total write errors encountered in Long Term Store",
    ["backend"],
)

# --- Production hardening metrics (added during PRR remediation) -----------

GUARDRAIL_SEMANTIC_FALLBACK_TOTAL = Counter(
    "agent_guardrail_semantic_fallback_total",
    "Semantic guardrail evaluations that fell back because the judge was unavailable",
    ["mode"],
)

REFLECTION_FALLBACK_TOTAL = Counter(
    "agent_reflection_fallback_total",
    "Reflector evaluations that could not use the LLM judge",
    ["reason"],
)

CHECKPOINT_PERSIST_ERRORS_TOTAL = Counter(
    "agent_checkpoint_persist_errors_total",
    "Checkpoint persistence failures",
    ["backend", "operation"],
)

RAG_EMBED_PROVIDER = Counter(
    "agent_rag_embed_provider_info",
    "Embedding provider selected at retriever construction (1 = selected)",
    ["type"],
)

RAG_RERANKER_MODE = Counter(
    "agent_rag_reranker_mode_info",
    "Reranker mode selected (1 = selected)",
    ["mode"],
)

TOKEN_COST_TOTAL = Counter(
    "agent_token_cost_total",
    "Estimated token cost in USD",
    ["model_alias", "token_type"],
)

COST_ALLOCATION_TOTAL = Counter(
    "agent_cost_allocation_total",
    "Estimated token cost attributed to client, department and environment",
    ["client_id", "department", "env"],
)

APPROVAL_GRANTS_TOTAL = Counter(
    "agent_approval_grants_total",
    "Signed HITL approval grants issued, verified or rejected",
    ["outcome"],
)

RATE_LIMIT_REJECTIONS_TOTAL = Counter(
    "agent_rate_limit_rejections_total",
    "Requests rejected by rate or token quota limits",
    ["layer"],
)

OBSERVABILITY_ERRORS_TOTAL = Counter(
    "agent_observability_errors_total",
    "Self-failures of observability subsystems",
    ["component"],
)
