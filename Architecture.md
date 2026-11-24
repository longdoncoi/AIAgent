# Agentic Platform — kiến trúc tham chiếu

> Phiên bản tài liệu: `v2.1` · cập nhật 2026-11-24. Đây là overview; chỉ đọc specification theo domain khi cần chi tiết.

## 1. Invariants

1. **Dependency một chiều**: `L1 API → L2 Supervisor → L3 Agents → L4 Tools / L5 RAG → L6 Memory / L7 Models`; `core/` là kernel độc lập.
2. **Stateless compute**: API và sub-agent không sở hữu session state; checkpoint/memory/cache nằm ở external stores.
3. **Protocol boundaries**: client dùng REST/SSE; agent dùng A2A; tool dùng MCP; model dùng OpenAI-compatible API qua LiteLLM; telemetry dùng OpenTelemetry.
4. **Async-first**: network/DB/cache/model I/O không chặn event loop; sync/CPU-heavy work chạy ngoài event loop.
5. **Resumable/replayable**: LangGraph state được checkpoint; HITL/retry/replan dùng cùng run/step identity.
6. **Defense in depth**: auth/tenant scope → input guard → policy/allowlist → step-scoped capability/HITL → output guard.
7. **Fail closed in production**: không local fallback khi nó bỏ qua transport isolation, tenant scope, authorization hoặc durability.
8. **Observable/evaluable**: trace context đi xuyên API → graph → A2A → MCP/RAG/LLM; prompt/routing/RAG change phải có eval phù hợp.

## 2. Layer map

```text
Clients
  │ REST / SSE
  ▼
L1 FastAPI: auth, rate limit, streaming, approvals
  │
  ▼
L2 LangGraph Supervisor: plan → HITL → dispatch/fan-out → reflect → finalize
  │ A2A
  ├── L3 ChatAgent (9100)
  ├── L3 RAGAgent (9101) ───────────────► L5 hybrid RAG / Qdrant
  └── L3 CodingAgent (9102) ── MCP ─────► L4 tools (9001–9003)
  │
  ├── L6 PostgreSQL / Redis memory and checkpoints
  └── L7 LiteLLM (4000) → vLLM (8001) / llama.cpp (8080, 8081)

Cross-cutting: L8 safety · L9 observability/evals · L10 Kubernetes/Temporal/GitOps
```

| Layer | Owner paths | Contract |
| --- | --- | --- |
| L1 API | `src/agentic/api/` | REST/SSE, auth principal, health, approval |
| L2 Supervisor | `src/agentic/orchestrator/` | `AgentState`, `Plan`, `Step`, graph transitions |
| L3 Agents/A2A | `src/agentic/agents/`, `src/agentic/a2a/` | A2A v1 `AgentCard`, JSON-RPC/SSE, persistent task state |
| L4 Tools | `src/agentic/tools/`, `configs/policies.yaml` | MCP schemas, allowlist, risk/capability |
| L5 Knowledge | `src/agentic/rag/` | ingestion, tenant-filtered retrieval, fusion/rerank |
| L6 Memory | `src/agentic/memory/`, `migrations/` | checkpoint, ownership, long-term memory |
| L7 Models | `src/agentic/llm/`, `configs/litellm_config.yaml` | model aliases, OpenAI-compatible gateway |
| L8 Safety | `src/agentic/safety/` | guardrails, policy, signed capabilities |
| L9 Quality | `src/agentic/observability/`, `evals/` | trace/metrics/eval gates |
| L10 Platform | `src/agentic/workflows/`, `deploy/`, `.github/workflows/` | durable jobs and deployment |

## 3. Request lifecycle

1. L1 authenticates the principal, creates a run-scoped state and resets transient approval/step state.
2. Planner produces a validated DAG of typed steps, agent targets, dependencies, risk and approval tools.
3. HITL interrupts before an unapproved high-risk step; checkpoint preserves resumable state.
4. Dispatch selects dependency-ready steps and fans out independent work over A2A.
5. Dispatch issues a short-lived capability bound to run, step, owner, agent and allowed tools. A2A executor verifies identity before constructing tool execution context.
6. MCP client and server independently enforce agent allowlist, risk/approval, S2S auth and capability claims.
7. RAG derives tenant/filter from trusted execution context before lexical/vector search, fusion and rerank.
8. Reflector completes, retries or replans from the latest outputs. Finalize emits only final SSE events after output guards.
9. Trace/metrics correlate the request across all boundaries; checkpoint and long-term writes preserve owner scope.

Chi tiết state/endpoint nằm ở [agent specification](.workflows/specifications/agent-specifications.md), [protocol/network](.workflows/specifications/protocols-networking.md) và [memory](.workflows/specifications/state-memory.md).

## 4. State, security and failure semantics

- Production graph lifecycle owns `AsyncPostgresSaver`; Redis serialized payloads require HMAC validation.
- A2A discovery dùng `/.well-known/agent-card.json`, RPC dùng JSON-RPC/SSE tại `/a2a`; production lưu task trong PostgreSQL `a2a_tasks`, vì vậy đổi replica không làm mất task lifecycle.
- Thread ownership is resolved server-side; client-provided identifiers không tự cấp quyền đọc checkpoint.
- High-risk approval applies to one pending step, not the whole run. Capability expiry/replay checks are enforced at A2A and MCP boundaries.
- A2A/MCP network calls yêu cầu S2S token và capability có chữ ký khóa theo owner/run/step/agent; web-search provider lỗi phải trả lỗi, tuyệt đối không tạo kết quả giả.
- Cache/retrieval keys include tenant and every result-affecting input. Shared documents require explicit metadata.
- Timeout/retry/circuit breaking must be bounded and observable. Fallback may preserve availability only when it preserves security and data consistency.
- CDC/ingestion resolves paths below the configured data root and keeps upsert/delete, persisted index and retriever lifecycle consistent.

Chi tiết: [safety/HITL](.workflows/specifications/safety-hitl.md) và [observability/evals](.workflows/specifications/observability-evals.md).

## 5. Runtime topology

| Service | Local port | Canonical config |
| --- | ---: | --- |
| FastAPI | `8000` | `src/agentic/core/config.py`, `Makefile` |
| LiteLLM / vLLM | `4000` / `8001` | `configs/litellm_config.yaml`, `deploy/serving/` |
| llama.cpp chat / embed | `8080` / `8081` | `deploy/serving/` |
| MCP code / repo / web | `9001` / `9002` / `9003` | `Makefile`, `deploy/` |
| A2A chat / RAG / coding | `9100` / `9101` / `9102` | `src/agentic/core/config.py`, `deploy/` |
| PostgreSQL / Redis / Qdrant / Temporal | `5432` / `6379` / `6333` / `7233` | `.env.example`, `deploy/` |

Deployment production dùng Kubernetes/Kustomize, service accounts, ExternalSecret, NetworkPolicy và Istio policy. Container sandbox chạy non-root, read-only filesystem và drop capabilities; code execution có giới hạn tài nguyên/thời gian. Manifests là nguồn sự thật, không phải bảng port này.

## 6. Source-of-truth và tài liệu chi tiết

| Câu hỏi | Nguồn cần đọc |
| --- | --- |
| Dependency/version nào thực sự dùng? | `pyproject.toml`, `uv.lock` |
| Lệnh build/test/serve? | `Makefile`, `.github/workflows/ci.yml` |
| Setting/URL mặc định? | `src/agentic/core/config.py`, `.env.example` |
| Tool nào được phép/rủi ro gì? | `configs/policies.yaml` |
| Eval gate hiện tại? | `configs/eval_thresholds.yaml` |
| Agent/state/A2A? | `.workflows/specifications/agent-specifications.md` |
| MCP/network/cache? | `.workflows/specifications/protocols-networking.md` |
| Persistence/memory? | `.workflows/specifications/state-memory.md` |
| Safety/HITL? | `.workflows/specifications/safety-hitl.md` |
| Trace/metric/eval? | `.workflows/specifications/observability-evals.md` |
| Release readiness? | `.workflows/reviews/REVIEW.md`, rồi checklist liên quan |

## 7. Quy tắc cập nhật

- Đổi layer, protocol, request lifecycle hoặc trust boundary: cập nhật tệp này và specification liên quan.
- Đổi implementation nội bộ không làm thay contract: cập nhật code/test; không cần chép vào overview.
- Đổi port/settings/policy/eval: cập nhật source-of-truth trước, rồi bảng tra cứu liên quan nếu cần.
- Quyết định có đánh đổi dài hạn: thêm ADR ngắn vào `.memory/architectureDecisions.md`.
- Không chép status sprint, kết quả test tạm thời hoặc danh sách file toàn repo vào kiến trúc.
