"""Prometheus metrics for Agentic Platform monitoring."""

from prometheus_client import Counter, Histogram

REQUEST_DURATION = Histogram(
    "agent_request_duration_seconds",
    "Time spent processing agent requests",
    ["component", "status"],
    buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
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

