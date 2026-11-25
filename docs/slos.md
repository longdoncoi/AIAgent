# Service Level Objectives (SLOs) & Latency Budgets

This document defines the production Service Level Objectives, error budgets,
and load-test targets for `agentic-platform` (verified against
[tests/load/locustfile.py](../tests/load/locustfile.py)).

## 1. Availability & Correctness SLIs

| SLI | Target (30-day rolling) | Measurement |
| :--- | :--- | :--- |
| API Availability (`/api/v1/chat`, `/api/v1/chat/stream`) | `>= 99.9%` non-5xx | `1 - (sum(rate(agent_request_duration_seconds_count{status="error"}[5m])) / sum(rate(agent_request_duration_seconds_count[5m])))` |
| Readiness Probe Convergence (`/readyz`) | `>= 99.95%` | K8s readiness probe success |
| Fail-Closed Security & RBAC Leaks | `0` tolerated | Release gate + red-team + multi-tenant regression suite |
| Checkpoint Persistence Durability | `0` unalerted failures | `increase(agent_checkpoint_persist_errors_total[5m]) == 0` |

## 2. Latency SLOs

| Workload Path | P50 | P95 | P99 | LLM Round-Trips |
| :--- | :--- | :--- | :--- | :--- |
| **Simple Conversational Fast-Path** (1 low-risk `chat` step) | `<= 1.2s` | `<= 2.5s` | `<= 4.0s` | Guardrail + Planner + ChatAgent (skips Reflector & synthesis Finalize) |
| **Streaming TTFT** (`/api/v1/chat/stream`) | `<= 0.8s` | `<= 1.8s` | `<= 3.0s` | `agent_time_to_first_token_seconds` |
| **RAG Knowledge Query** (`rag_agent` + citations) | `<= 2.5s` | `<= 5.0s` | `<= 8.0s` | Guardrail + Planner + RAGAgent + Reflector + Finalize |
| **Coding / Multi-Step DAG** | `<= 6.0s` | `<= 15.0s` | `<= 25.0s` | Full Supervisor DAG + sandbox tool execution |

## 3. Baseline Load Verification Target (Staging)

Before opening GA traffic, staging benchmarks via `tests/load/locustfile.py`
must sustain:

- Release load gate: `100` concurrent users at `spawn-rate=10` for `>= 5m`
- Soak gate: `>= 50` concurrent users at `spawn-rate=5` for `>= 15m`
- Error rate: `< 0.1%`; P95 `< 2.5s`; P99 `< 4.5s`. The Locust quitting hook enforces these limits and exits non-zero on breach.
- Operational alert threshold remains `< 2.0%` (`HighAPIErrorRate`); it is not a release acceptance threshold.
- `ModelCircuitBreakerTripped` (both client breaker and `agent_model_proxy_fallback_total`): `0` unexpected trips under nominal load.
