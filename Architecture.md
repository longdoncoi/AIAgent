# ARCHITECTURE.md — Kiến Trúc Tham Chiếu Hệ Thống Agentic AI Doanh Nghiệp (Enterprise Reference Architecture)

> **Tài liệu đặc tả kiến trúc chính thức** của dự án `agentic-platform`.  
> Hệ thống Multi-Agent Doanh Nghiệp Phân Cấp: LangGraph Orchestrator · Sub-Agents A2A · MCP Tools · LlamaIndex RAG · LiteLLM / vLLM / llama.cpp · Caching đa tầng · Temporal · Kubernetes.  
> **Phiên bản**: `v2.0.0-PROD` · **Cập nhật**: 19/11/2026

---

## 0. Nguyên Tắc Thiết Kế Cốt Lõi (Architectural Invariants)

Hệ thống được xây dựng trên 8 nguyên tắc kỹ thuật bất biến:

1. **Phụ thuộc một chiều (Dependency Rule)**:
   Tầng trên chỉ gọi tầng dưới qua interface/protocol chuẩn hóa, tuyệt đối không import hay phụ thuộc ngược (`core/` là kernel độc lập, không import bất kỳ tầng nào khác).
2. **Local-First & Model Agnostic**:
   Ưu tiên triển khai tự chủ trên hạ tầng on-premise/self-hosted (vLLM cho GPU, llama.cpp cho CPU/edge, BGE embedding). Trừu tượng hóa hoàn toàn qua LiteLLM Gateway và `ModelCircuitBreaker`; việc thay đổi nhà cung cấp hoặc model backend được thực hiện qua cấu hình `configs/litellm_config.yaml`, tuyệt đối không sửa đổi mã nguồn nghiệp vụ của Agent.
3. **Dịch vụ phi trạng thái (Stateless Services & Externalized State)**:
   Mọi API pod và Sub-Agent pod đều hoàn toàn stateless để phục vụ việc scale ngang tự động. Toàn bộ trạng thái phiên làm việc (Active State, Execution DAG) do Supervisor quản lý và lưu trữ ngoại vi (Postgres Checkpointer, Redis, Qdrant Vector Store).
4. **Chuẩn hóa giao thức tại mọi ranh giới (Standard Protocols)**:
   - **Agent ↔ Agent**: Giao thức **A2A (Agent-to-Agent)** qua HTTP/gRPC với `AgentCard` định nghĩa tường minh kỹ năng (capabilities/skills).
   - **Agent ↔ Tool**: Giao thức **MCP (Model Context Protocol)** với các FastMCP servers chạy độc lập trong sandbox cô lập.
   - **Agent ↔ Model**: Chuẩn **OpenAI-compatible API** qua LiteLLM Gateway và client `src/agentic/llm/gateway.py`.
   - **Hệ thống ↔ Client**: Chuẩn **REST + Server-Sent Events (SSE)** hỗ trợ token streaming trực tiếp.
   - **Telemetry**: Chuẩn **OpenTelemetry (OTLP)** cho metrics/traces kết hợp LangSmith.
5. **Phòng vệ theo chiều sâu (Defense in Depth)**:
   Áp dụng kiểm soát an ninh đa tầng: Xác thực & Phân quyền ở Edge (JWT/RBAC) $\rightarrow$ Input Guard (PII, Prompt Injection, Obfuscation decoding) $\rightarrow$ Tool Guard (Allowlist, schema validation, HITL interrupt cho tác vụ rủi ro cao) $\rightarrow$ Output Guard (StreamGuard buffer lọc rò rỉ dữ liệu).
6. **Khả sát toàn diện theo mặc định (Observable by Default)**:
   Một `trace_id` duy nhất được lan truyền xuyên suốt từ API request $\rightarrow$ Orchestrator $\rightarrow$ Sub-Agent $\rightarrow$ Tool Call $\rightarrow$ RAG Ingestion $\rightarrow$ LLM generation, đồng bộ giữa OpenTelemetry, Jaeger và LangSmith.
7. **Đo lường & Kiểm định chất lượng định lượng (Evaluable & Quality Gates)**:
   Đánh giá tự động chất lượng sinh văn bản và điều phối trong CI/CD bằng **DeepEval** và **RAGAS framework** (Routing Accuracy, Groundedness, Faithfulness, Context Precision/Recall), chặn đứng mã nguồn nếu dưới ngưỡng cam kết.
8. **Khả năng chịu lỗi & Tự phục hồi (Fail-Safe & Circuit Breaking)**:
   Tích hợp timeout, retry lũy thừa, fallback tự động từ GPU (vLLM) sang CPU (llama.cpp) khi quá tải hoặc sự cố, Human-in-the-Loop (HITL) cho các thao tác rủi ro cao và durable execution qua Temporal workflows.

---

## 1. Kiến Trúc Phân Tầng (Layered Architecture)

```
 CLIENTS      Web UI · Mobile App · CLI · Slack · External Systems
    │
    ▼ HTTPS + Bearer JWT / OIDC
┌─ L1 INTERFACE & EDGE ─────────────────────────────────────────────────────────┐
│  FastAPI · SSE Streaming · AuthN (JWT) · AuthZ (RBAC) · Rate Limiting · CORS   │
│  Request-ID tracking · Prometheus Endpoint · Health & Readiness Probes        │
├─ L2 ORCHESTRATION (LangGraph Supervisor) ────────────────────────────────────┤
│  Supervisor · Planner (DAG) · Dispatch (Fan-out) · ReAct · Reflection (Critique)│
│  State Checkpointing · HITL Interrupt & Resume · Retry Policy                 │
├─ L3 SUB-AGENTS (A2A Protocol) ───────────────────────────────────────────────┤
│  ChatAgent (Port 9100)  ·  RAGAgent (Port 9101)  ·  CodingAgent (Port 9102)   │
├────────────────────┬─────────────────────────┬───────────────────────────────┤
│ L4 TOOLS (MCP)     │ L5 KNOWLEDGE (RAG)      │ L6 MEMORY & PERSISTENCE       │
│ FastMCP Servers:   │ LlamaIndex Engine:      │ Short-Term (STM):             │
│ - code_exec (9001) │ - Hierarchical chunking │ - PostgresSaver (Connection   │
│ - repo_server(9002)│ - Hybrid (Dense + BM25) │   Pool tương thích PgBouncer) │
│ - web_search (9003)│ - Neo4j Knowledge Graph │ - Distributed RedisSaver      │
│ Isolated Sandbox   │ - Cross-Encoder Rerank  │ Long-Term (LTM):              │
│ Tool Allowlist     │ - Auto-merging Context  │ - Semantic / Episodic / Proc  │
├────────────────────┴─────────────────────────┴───────────────────────────────┤
│ L7 MODEL GATEWAY & SERVING ──────────────────────────────────────────────────┤
│ LiteLLM Gateway (Port 4000: Router, Quota, Budget, Failover)                 │
│ ├─ Primary GPU: vLLM Engine (8001 Qwen2.5-72B; 8002 Qwen2.5-Coder-32B)        │
│ └─ Fallback / CPU: llama.cpp (Port 8080: Chat / Port 8081: BGE Embeddings)  │
├──────────────────────────────────────────────────────────────────────────────┤
│ L10 DURABLE PLATFORM & INFRASTRUCTURE ───────────────────────────────────────┤
│ Kubernetes · ArgoCD · Temporal (Port 7233) · PostgreSQL · Qdrant · Redis     │
╞══════════════════════════════════════════════════════════════════════════════╡
│ CROSS-CUTTING:                                                               │
│ L8 SAFETY: Guardrails · PII Redaction · Tool Policy · StreamGuard Buffer     │
│ L9 OBSERVABILITY & QUALITY: OpenTelemetry · LangSmith · Prometheus · RAGAS   │
└──────────────────────────────────────────────────────────────────────────────┘
```

### Bảng Phân Nhiệm Các Tầng & Công Nghệ Cốt Lõi

| Tầng | Vai Trò & Trách Nhiệm | Công Nghệ Chủ Chốt | Thư Mục Mã Nguồn |
| :--- | :--- | :--- | :--- |
| **L1 Interface** | Cổng tiếp nhận duy nhất: AuthN/Z, Rate Limit, SSE Streaming, HITL Approvals | FastAPI, PyJWT, SlidingWindowRateLimiter, Pydantic v2 | `src/agentic/api/` |
| **L2 Orchestration** | Tổng chỉ huy: Phân rã bài toán, lập kế hoạch, ReAct loop, tự phản tỉnh, dừng chờ duyệt | LangGraph, Pydantic State | `src/agentic/orchestrator/` |
| **L3 Sub-Agents** | Các chuyên gia tác tử độc lập, giao tiếp qua A2A-inspired REST/SSE profile nội bộ (chưa phải JSON-RPC A2A SDK) | FastAPI REST/SSE, LangGraph ReAct | `src/agentic/agents/`, `src/agentic/a2a/` |
| **L4 Tools (MCP)** | Thực thi công cụ chuyên biệt trong sandbox cách ly an toàn | FastMCP, Docker/gVisor | `src/agentic/tools/` |
| **L5 Knowledge** | Truy xuất tri thức hybrid, Knowledge Graph triples và reranking; Neo4j là backend tùy chọn và mặc định tắt trong production hiện tại | LlamaIndex, Qdrant, NetworkX/BM25; Neo4j optional | `src/agentic/rag/` |
| **L6 Memory** | Quản lý bộ nhớ ngắn hạn (phiên) và bộ nhớ dài hạn (tri thức tích lũy) | PostgresSaver, RedisSaver, Alembic | `src/agentic/memory/` |
| **L7 Model Gateway** | Định tuyến mô hình nền tảng, quản lý quota và circuit breaker dự phòng | LiteLLM, vLLM (GPU), llama.cpp (CPU) | `src/agentic/llm/`, `deploy/serving/` |
| **L8 Safety** | Kiểm soát an toàn đầu vào, phân quyền tool và chống rò rỉ dữ liệu đầu ra | Regex PII, Policy Engine, StreamGuard | `src/agentic/safety/` |
| **L9 Observability** | Giám sát phân tán, theo dõi token, đo lường độ trễ và chấm điểm chất lượng | OpenTelemetry, LangSmith, Prometheus, RAGAS | `src/agentic/observability/`, `evals/` |
| **L10 Platform** | Điều phối workflow bền bỉ (durable), hạ tầng chứa và triển khai tự động | Temporal Server, K8s, Kustomize, ArgoCD | `src/agentic/workflows/`, `deploy/` |

---

## 2. Bản Đồ Yêu Cầu Kỹ Thuật → Giải Pháp & Vị Trí Mã Nguồn

| Yêu Cầu Kỹ Thuật | Giải Pháp Kiến Trúc | Tệp Mã Nguồn / Cấu Hình Chịu Trách Nhiệm |
| :--- | :--- | :--- |
| **Tự Chủ Mô Hình (Local-First)** | Cụm vLLM (GPU) kết hợp llama.cpp (CPU) chuẩn OpenAI-compatible, LiteLLM định tuyến | `configs/litellm_config.yaml`<br>`src/agentic/llm/gateway.py` |
| **Token Streaming Thời Gian Thực** | LLM Streaming $\rightarrow$ LangGraph state stream $\rightarrow$ FastAPI SSE (`/api/v1/chat/stream`) | `src/agentic/api/routes/chat.py`<br>`src/agentic/orchestrator/nodes/finalize.py` |
| **RAG: Phân Cấp & Auto-Merging** | `HierarchicalNodeParser` (parent 1024, child 256) + `AutoMergingRetriever` | `src/agentic/rag/ingestion.py`<br>`src/agentic/rag/retriever.py` |
| **RAG: Hybrid Search & KG Fusion** | Dense Vector (Qdrant) + BM25 Lexical + NetworkX Graph RRF + Cross-Encoder; Neo4j chỉ bật khi được cấu hình và kiểm định riêng | `src/agentic/rag/retriever.py`<br>`src/agentic/rag/kg.py`, `fusion.py`, `reranker.py` |
| **State Checkpointing & STM** | LangGraph `PostgresSaver` (Connection Pool PgBouncer) & `RedisSaver` bất đồng bộ | `src/agentic/memory/short_term.py`<br>`src/agentic/memory/redis_saver.py` |
| **Long-Term Memory (LTM)** | Lưu trữ phân tầng Semantic (Fact), Episodic (Lịch sử), Procedural (Kinh nghiệm) | `src/agentic/memory/long_term.py`<br>`src/agentic/memory/consolidation.py` |
| **Multi-Tier Caching** | Two-Tier L1 Memory + L2 Redis cho RAG Query và Prompt Completion; Prefix Cache GPU | `src/agentic/cache/rag_cache.py`<br>`src/agentic/cache/prompt_cache.py`, `kv_cache.py` |
| **Giám Sát Phân Tán (Tracing)** | OpenTelemetry SDK xuất OTLP kết hợp native LangSmith Tracing & feedback metrics | `src/agentic/observability/tracing.py`<br>`src/agentic/observability/langsmith_tracer.py` |
| **Kiểm Thử Chất Lượng (Eval Gate)** | DeepEval & RAGAS Framework đo lường Faithfulness, Relevance, Precision, Recall | `evals/deepeval_runner.py`<br>`evals/ragas_runner.py`, `evals/run_eval.py` |
| **Human-In-The-Loop (HITL)** | Tự động phát hiện tác vụ rủi ro cao, ngắt graph (`interrupt`), chờ duyệt qua `/approvals` | `src/agentic/orchestrator/nodes/hitl.py`<br>`src/agentic/api/routes/approvals.py` |
| **Phòng Vệ An Ninh (Guardrails)** | Lọc PII (CCCD, SĐT, Email), giải mã Obfuscation, chặn Prompt Injection, StreamGuard | `src/agentic/safety/guardrails.py`<br>`src/agentic/safety/policy.py` |

---

## 3. Cấu Trúc Thư Mục Toàn Repo (TreeView Toàn Diện)

```
agentic-platform/
├── pyproject.toml                 # Dependency lock, công cụ linting & testing
├── Makefile                       # make dev | test | eval | ingest | serve-*
├── .env.example                   # Biến môi trường mẫu cho hạ tầng và credentials
├── README.md                      # Hướng dẫn khởi động nhanh dự án
├── AGENTS.md                      # Đặc tả hệ thống Multi-Agent của nền tảng (Product Specs)
├── ARCHITECTURE.md                # Tài liệu kiến trúc tham chiếu chuẩn của toàn hệ sinh thái
│
├── .context/                      # Ngữ cảnh & chỉ dẫn cốt lõi cho AI Coding Agent (agent-instructions.md)
├── .skills/                       # Kho kịch bản thực thi tác vụ (Procedural Runbooks)
├── .memory/                       # Hệ thống bộ nhớ ngữ cảnh bền vững (Agent Persistent Memory)
├── .workflows/                    # Quy trình thao tác chuẩn (SOPs), Đặc tả & Đánh giá PRR
│   ├── specifications/            # 6 đặc tả domain (Spec, Protocols, Memory, Safety, Evals...)
│   └── reviews/                   # PRR Dashboard (REVIEW.md) & 18 tệp kiểm định chuyên biệt
│
├── configs/                       # Cấu hình độc lập ngoài mã nguồn
│   ├── litellm_config.yaml        # Danh mục model, routing, alias, fallbacks, budget
│   ├── policies.yaml              # Phân loại mức độ rủi ro công cụ, HITL policy, timeout
│   ├── eval_thresholds.yaml       # Ngưỡng chặn CI/CD (Faithfulness, Relevance, Precision)
│   └── prompts/                   # Hệ thống prompt được quản lý phiên bản
│       ├── supervisor.md          # Prompt chỉ huy điều phối
│       ├── planner.md             # Prompt phân rã bài toán
│       ├── reflector.md           # Prompt phản tỉnh & tự sửa sai
│       ├── chat_agent.md          # Prompt giao tiếp ngữ cảnh
│       ├── rag_agent.md           # Prompt trả lời căn cứ tài liệu
│       └── coding_agent.md        # Prompt lập trình & gỡ lỗi
│
├── src/agentic/
│   ├── core/                      # Kernel dùng chung, KHÔNG phụ thuộc tầng khác
│   │   ├── config.py              # Pydantic Settings quản lý biến môi trường
│   │   ├── errors.py              # Hệ thống mã lỗi: AgentError, GuardrailViolation...
│   │   └── schemas.py             # DTO chuẩn: ChatRequest, Decision, Step...
│   │
│   ├── api/                       # L1 Interface
│   │   ├── main.py                # FastAPI lifecycle, middleware, CORS, routers
│   │   ├── deps.py                # Dependency injection: Auth JWT, graph state
│   │   ├── middleware.py          # Prometheus metrics, Request-ID, IP tracking
│   │   └── routes/
│   │       ├── chat.py            # Endpoint chat non-streaming & SSE token streaming
│   │       ├── approvals.py       # Endpoint tiếp nhận quyết định duyệt HITL
│   │       └── health.py          # Liveness, readiness, health check endpoints
│   │
│   ├── orchestrator/              # L2 Supervisor (LangGraph Engine)
│   │   ├── state.py               # AgentState, Plan, Step schema và reducer
│   │   ├── graph.py               # Lắp ráp StateGraph điều phối tổng
│   │   └── nodes/
│   │       ├── planner.py         # Lập DAG kế hoạch bước thực thi
│   │       ├── dispatch.py        # Điều phối song song (Fan-out) và tuần tự
│   │       ├── hitl.py            # Ngắt đồ thị (interrupt) khi rủi ro cao
│   │       ├── reflector.py       # Kiểm định chất lượng phản hồi, tự sửa lỗi
│   │       └── finalize.py        # Tổng hợp kết quả và chuẩn bị stream đầu ra
│   │
│   ├── agents/                    # L3 Chuyên gia Sub-agents
│   │   ├── base.py                # Vòng lặp ReAct chuẩn (`build_react_graph`)
│   │   ├── chat/                  # ChatAgent: Đàm thoại thông thường (Port 9100)
│   │   ├── rag/                   # RAGAgent: Tra cứu chính sách nội bộ (Port 9101)
│   │   └── coding/                # CodingAgent: Sinh code & sandbox test (Port 9102)
│   │
│   ├── a2a/                       # Giao thức Agent-to-Agent
│   │   ├── client.py              # A2A Async Client có HTTP Connection Pooling
│   │   ├── executor.py            # Bọc LangGraph StateGraph thành chuẩn A2A Executor
│   │   └── cards.py               # AgentCard khai báo năng lực và kỹ năng
│   │
│   ├── tools/                     # L4 FastMCP Tools
│   │   ├── mcp_client.py          # Client kết nối MCP Servers, áp dụng Allowlist
│   │   └── servers/
│   │       ├── code_exec_server.py # MCP Port 9001: Chạy Python/Bash trong sandbox
│   │       ├── repo_server.py      # MCP Port 9002: Thao tác tệp, symbol, git diff
│   │       └── web_search_server.py# MCP Port 9003: Tìm kiếm thông tin ngoài web
│   │
│   ├── rag/                       # L5 LlamaIndex + Knowledge Graph
│   │   ├── ingestion.py           # Ingest tài liệu, Hierarchical Node Parsing
│   │   ├── retriever.py           # Hybrid search: Qdrant Dense + BM25 Lexical
│   │   ├── kg.py                  # Quản lý Knowledge Graph Neo4j / NetworkX
│   │   ├── fusion.py              # Thuật toán Reciprocal Rank Fusion (RRF)
│   │   ├── reranker.py            # Cross-Encoder (BAAI/bge-reranker-base)
│   │   └── tools.py               # search_policy() bất đồng bộ cho RAGAgent
│   │
│   ├── memory/                    # L6 Quản lý Trạng Thái & Bộ Nhớ
│   │   ├── short_term.py          # PostgresSaver (PgBouncer pool), context trimming
│   │   ├── redis_saver.py         # Distributed Redis Checkpointer với HMAC-SHA256
│   │   ├── long_term.py           # Bộ nhớ Semantic, Episodic, Procedural
│   │   ├── consolidation.py       # Đúc kết kinh nghiệm sau mỗi phiên làm việc
│   │   └── migrations.py          # Khởi tạo và kiểm tra schema database
│   │
│   ├── cache/                     # Hệ Thống Bộ Đệm Đa Tầng (Multi-Tier Caching)
│   │   ├── rag_cache.py           # Two-Tier L1 Memory + L2 Redis Semantic Query Cache
│   │   ├── prompt_cache.py        # Two-Tier L1 Memory + L2 Redis Exact Completion Cache
│   │   └── kv_cache.py            # Tối ưu hóa GPU KV-Cache & Prefix Caching
│   │
│   ├── llm/                       # L7 Model Gateway Client
│   │   └── gateway.py             # get_llm(): LiteLLM routing, Circuit Breaker, token metrics
│   │
│   ├── safety/                    # L8 An Ninh & Phòng Vệ
│   │   ├── guardrails.py          # Quét PII, Obfuscation, Prompt Injection, StreamGuard
│   │   └── policy.py              # Thẩm định quyền gọi tool và đánh giá risk level
│   │
│   ├── observability/             # L9 Giám Sát Phân Tán
│   │   ├── tracing.py             # OpenTelemetry TracerProvider & OTLP exporter
│   │   ├── langsmith_tracer.py    # LangSmith tracing, dataset sync, runtime feedback
│   │   └── metrics.py             # Prometheus metrics: token, latency, hitl count
│   │
│   └── workflows/                 # L10 Durable Workflows (Temporal)
│       ├── agent_workflow.py      # Workflow điều phối các tác vụ kéo dài
│       ├── rag_workflow.py        # Workflow Ingestion đồng bộ dữ liệu nền
│       └── worker.py              # Background worker lắng nghe hàng đợi Temporal
│
├── evals/                         # L9 Đánh Giá Định Lượng & Quality Gate
│   ├── datasets/                  # golden_v1.jsonl (100), rag_qa.jsonl (50), red_teaming.jsonl (60)
│   ├── evaluators.py              # LLM-as-a-judge: Routing, Groundedness, Faithfulness
│   ├── deepeval_runner.py         # Suite đo lường chất lượng DeepEval
│   ├── ragas_runner.py            # Bộ kiểm thử 4 chỉ số chuyên sâu RAGAS framework
│   ├── red_teaming_runner.py      # Đánh giá phòng vệ Jailbreak/Injection/Toxicity (60 ca)
│   ├── release_gate.py            # Cổng chất lượng release nghiêm ngặt (cấm proxy/heuristic)
│   └── run_eval.py                # Script chạy `--mode smoke` hoặc `--mode release` trong CI/CD
│
├── migrations/                    # Quản lý tiến hóa cơ sở dữ liệu (Alembic)
│   ├── versions/                  # Các file migration phiên bản (001..004_audit_logs_seq_and_checkpoints.py)
│   ├── env.py                     # Cấu hình môi trường kết nối Alembic (ưu tiên POSTGRES_MIGRATION_URI)
│   └── script.py.mako             # Template sinh mã migration tự động
├── alembic.ini                    # Cấu hình kết nối cơ sở dữ liệu cho Alembic
│
├── data/                          # Dữ liệu phục vụ kiểm thử và RAG
│   ├── raw/                       # Tài liệu gốc (và `tenants/<id>/` cho tài liệu cô lập từng tenant)
│   └── index/                     # Index lưu trữ cục bộ (FAISS / docstore cache)
│
├── docs/                          # Tài liệu vận hành & SLO
│   ├── slos.md                    # Mục tiêu SLO, TTFT và tải benchmark staging
│   └── runbooks/                  # gpu_failure.md, rollback.md
│
├── tests/                         # Hệ thống kiểm thử tự động
│   ├── unit/                      # Kiểm thử đơn vị: guardrails, caching, core, state
│   ├── integration/               # Kiểm thử tích hợp: A2A agents, orchestrator, MCP tools
│   ├── e2e/                       # Kiểm thử luồng toàn vẹn: REST chat, SSE, luồng duyệt HITL
│   └── load/locustfile.py         # Kiểm thử tải đồng thời cao (Stress & Concurrency)
│
├── scripts/                       # ingest.py, seed_eval.py, migrate_db.py, verify_audit_chain.py, restore_drill.py, generate_prr_evidence.py
├── deploy/                        # Triển khai hạ tầng Production
│   ├── docker/                    # Dockerfile.api, Dockerfile.agent, Dockerfile.mcp, compose
│   ├── serving/                   # vllm.sh, llamacpp-chat.sh (--jinja), llamacpp-embed.sh (--embedding)
│   ├── k8s/                       # Kustomize base (API/Agents/MCP, serving, datastores, Ingress, NetworkPolicy, Sync Waves)
│   ├── overlays/production/       # Production patches + KEDA vLLM schedule 1..4 replicas
│   ├── monitoring/alerts.yaml     # Canonical Prometheus rules, gồm GPUOOMAlert
│   └── otel/otel-collector.yaml   # Cấu hình pipeline OTLP Collector
└── .github/workflows/ci.yml       # CI/CD: lint/test/eval → secret/CVE scan → SBOM/provenance → signed images
```

---

## 4. Luồng Xử Lý Yêu Cầu End-to-End & Token Streaming SSE

Quy trình xử lý một yêu cầu hội thoại hoàn chỉnh qua 12 bước có kiểm soát chặt chẽ:

```
[Client] ──► (1. POST /api/v1/chat/stream) ──► [L1 FastAPI Edge]
                                                    │
                                     (2. JWT / RBAC / Rate Limit)
                                                    │
                                                    ▼
                                            [L8 Input Guard]
                                       (3. PII, Injection check)
                                                    │ (Hợp lệ)
                                                    ▼
                                           [L2 LangGraph State]
                                      (4. Load Checkpoint & STM)
                                                    │
                                                    ▼
                                            [L2 Planner Node]
                                       (5. Phân rã DAG các bước)
                                                    │
                                                    ▼
                                           [L2 Dispatch Node]
                                  (6. Gửi lệnh qua A2A Protocol)
                                                    │
                                                    ▼
                                           [L3 Sub-Agent ReAct]
                                  (7. Suy luận & Chọn MCP Tools)
                                                    │
                          ┌─────────────────────────┴─────────────────────────┐
                          ▼                                                   ▼
                [Thao tác rủi ro cao]                               [Thao tác rủi ro thấp]
             (8a. Kích hoạt L2 HITL)                             (8b. Gọi MCP Server / RAG)
                 `interrupt()`                                                │
             Chờ duyệt tại /api/v1/approvals                                  ▼
                          │                                           [L2 Reflector Node]
                          └─────────────────► (Tiếp tục) ─────────► (9. Phản tỉnh & Critique)
                                                                              │ (Đạt chuẩn)
                                                                              ▼
                                                                     [L8 Output Guard]
                                                                (10. StreamGuard Buffer)
                                                                              │
                                                                              ▼
                                                                     [L1 SSE Streaming]
                                                               (11. Đẩy Token về Client)
                                                                              │
                                                                              ▼
                                                                    [L6 Memory & L9 OTel]
                                                               (12. Ghi Checkpoint & LTM)
```

### Chi Tiết 12 Bước Vận Hành:

1. **Tiếp nhận yêu cầu (Client Request)**:
   Client gửi request tới `POST /api/v1/chat/stream` kèm header `Authorization: Bearer <JWT>` (mang `sub`, `tenant_id`, `roles`), body `{thread_id, message}`.
2. **Kiểm soát tại Edge (L1 Edge & Security)**:
   FastAPI middleware gán `X-Request-ID`, kiểm tra IP allowlist/rate limit, xác thực chữ ký JWT, kiểm tra phạm vi quyền (scope `chat:invoke`). Thread ID được namespace dạng `{tenant_id}:{user_id}:{thread_id}` chống truy cập chéo.
3. **Phòng vệ đầu vào (L8 Input Guard)**:
   Quét phát hiện PII (số CCCD, số thẻ, số điện thoại), giải mã chuỗi mã hóa che giấu (Base64/Hex/Unicode homoglyphs), phát hiện dấu hiệu Prompt Injection / Jailbreak. Vi phạm lập tức trả mã lỗi 400 kèm câu từ chối chuẩn, không chuyển vào Graph.
4. **Khôi phục trạng thái & Reset theo lượt (L2 State Restoration)**:
   Supervisor nạp checkpoint từ PostgresSaver / RedisSaver dựa trên `thread_id`. Khi sang lượt hội thoại mới (`last_turn_human_message_id` thay đổi hoặc qua `build_turn_input()`), Planner và API tự động reset toàn bộ trạng thái riêng của lượt trước (`reflection_count`, `reflection_notes`, `degraded`, `approved_tool_call_digests`, `pending_tool_approval`), tránh lặp phản tỉnh hoặc bỏ qua Reflector từ lượt 3. Đồng thời đọc LTM (Semantic Profile) để đưa ngữ cảnh người dùng vào prompt.
5. **Lập kế hoạch phân rã (L2 Planner)**:
   Planner node phân tích yêu cầu thành danh sách bước (`Plan` gồm các `Step`). `_apply_risk_policy` chỉ leo thang `risk_level: high` theo từ khóa trên các agent có khả năng đột biến (`coding` hoặc tool gated) nhằm tránh approval fatigue ở các câu hỏi `chat`/`rag`. Các step độc lập được cấu hình để thực thi song song (`fan-out`).
6. **Điều phối qua chuẩn A2A (L2 Dispatch)**:
   Supervisor gửi payload bất đồng bộ qua A2A Client (`http://localhost:910x/a2a`) tới Sub-Agent tương ứng với `idempotency_key` băm theo `(step.id, step.agent, tenant_id, owner_id, thread_id, turn_id, step_fingerprint)` để không bao giờ chia sẻ kết quả cache A2A chéo tenant hoặc chéo user.
7. **Vòng lặp ReAct tại Sub-Agent (L3 Sub-Agent)**:
   Sub-Agent thực hiện vòng lặp Suy luận - Hành động (ReAct) qua `build_react_graph()` (`ToolNode` giữ nguyên `ToolCallApprovalRequired`, `PermissionError`, `GuardrailViolation`, `GraphInterrupt` không bị nuốt thành `ToolMessage`), gọi tool qua MCP Client (`mcp_client.py` hỗ trợ `httpx2` + Streamable-HTTP).
8. **Thẩm định an toàn công cụ & HITL (L8 Tool Guard & L2 HITL)**:
   - *Công cụ rủi ro thấp* (tra cứu tri thức, đọc file tạm): Thực thi tức thì trên MCP Server hoặc RAG pipeline (Hybrid Dense + BM25 + KG Auto-merge + Cross-encoder rerank).
   - *Công cụ rủi ro cao* (sửa file nguồn `repo_write_file`, thực thi Python `code_exec_python`, thực thi lệnh bash hệ thống `code_exec_bash`, xóa dữ liệu, hay tool chưa khai báo theo cơ chế default-deny): Gán `risk_level: high` theo từng tool cụ thể, kích hoạt node `hitl.py` hoặc phê duyệt 2 pha ở mức tool-call (`pending_tool_approval` với `tool_name` và `tool_arguments` cụ thể) rồi gọi `interrupt()` (chống rò gauge `HITL_PENDING_APPROVALS` khi node replay). Graph tạm dừng, lưu checkpoint vào Postgres (`AsyncConnectionPool` + `AsyncPostgresSaver`), phát sự kiện `interrupt` qua SSE và chờ phê duyệt qua endpoint `/api/v1/approvals` (kiểm tra khớp `tenant_id` và four-eyes ở `staging`/`production`, hỗ trợ xoay vòng khóa `kid`).
9. **Nhánh nhanh hội thoại & Tự phản tỉnh (L2 Fast-Path / Reflector)**:
   - Nếu chỉ có 1 bước `chat` `risk_level: low` và bật `enable_chat_fast_path=True`, graph đi thẳng tới `fast_finalize` (bỏ qua Reflector và LLM synthesis dư thừa để đạt SLO độ trễ tại `docs/slos.md`).
   - Các trường hợp khác đi qua Node Reflector đối chiếu câu trả lời từ Sub-Agent với yêu cầu gốc và tài liệu ngữ cảnh:
     - *Nếu đạt*: Chuyển sang bước tổng hợp (`finalize.py`).
     - *Nếu chưa đạt/ảo giác*: Tăng `reflection_count`, ghi chú phản tỉnh (`reflection_notes`) và yêu cầu Sub-Agent chạy lại với chỉ dẫn khắc phục.
10. **Lọc dữ liệu đầu ra (L8 Output Guard - StreamGuard Buffer)**:
    Sử dụng kỹ thuật cửa sổ trượt (Sliding Window Hold Buffer) với kích thước `stream_hold_chars` (mặc định 48 ký tự). Giữ lại đoạn đệm cuối để các chuỗi PII (kể cả số điện thoại VN có khoảng trắng, API key chạy trước phone) không bị chia tách giữa các chunk token trước khi đẩy ra ngoài.
11. **Truyền luồng sự kiện (L1 SSE Streaming)**:
    FastAPI đẩy trực tiếp các Server-Sent Events về Client theo chuẩn: `token` (văn bản sinh), `tool_call` (tên và tham số công cụ), `tool_result`, `interrupt` (yêu cầu duyệt), và `done`.
12. **Ghi nhận trạng thái, Audit Chain & Đo lường (Post-Execution)**:
    Sau khi chuỗi streaming hoàn tất:
    - Trạng thái được ghi vào PostgreSQL Checkpointer (`checkpoint_hmac_secret` hỗ trợ xoay vòng via `checkpoint_hmac_previous_secrets`).
    - Nhật ký audit hash-chained có thể kiểm chứng tính toàn vẹn qua `scripts/verify_audit_chain.py`.
    - Consolidation node trích xuất sự kiện quan trọng ghi vào Long-Term Memory; spans phân tán và metrics (`agent_model_proxy_fallback_total`, `HITL_PENDING_APPROVALS`) được đẩy về Jaeger, LangSmith và Prometheus.

---

## 5. Phục Vụ Mô Hình (Model Serving), Circuit Breaker & Runbook Sự Cố GPU

### 5.1. Kiến Trúc Model Gateway
Hệ thống sử dụng kiến trúc định tuyến hai lớp:
1. **Lớp Gateway Ngoài (LiteLLM Gateway - Port 4000)**:
   Phân giải alias (`reasoning-llm`, `fast-llm`, `coding-llm`) sang endpoint cụ thể với thuật toán cân bằng tải `least-busy` và cấu hình retry tự động. Khi LiteLLM tự fallback phía server sang model dự phòng (`llama-chat-backup` / `llama-server`), `TokenMetricsCallbackHandler` tự phát hiện tên model trả về để tăng `agent_model_proxy_fallback_total`, kích hoạt alert `ModelCircuitBreakerTripped` và cập nhật bộ ngắt mạch cục bộ.
2. **Lớp Bảo Vệ Cục Bộ (ModelCircuitBreaker tại `src/agentic/llm/gateway.py`)**:
   Theo dõi tỷ lệ lỗi kết nối. Nếu cụm GPU gặp sự cố liên tiếp 3 lần (`failure_threshold = 3`), Circuit Breaker tự động chuyển trạng thái `OPEN` và bẻ hướng toàn bộ traffic sang cụm CPU qua LiteLLM alias dự phòng (`llama-chat-backup` trỏ tới `llama.cpp` port 8080) trong thời gian chờ phục hồi (recovery window).

### 5.2. Runbook Xử Lý Sự Cố Cụm Phục Vụ Mô Hình (GPU Failure Runbook)

> **Mục tiêu**: Khôi phục năng lực phục vụ của cụm GPU trong thời gian dưới 5 phút, bảo đảm hệ thống luôn duy trì khả năng phản hồi tối thiểu trên CPU mà không gây gián đoạn phiên người dùng.

```
                  ┌──────────────────────────────┐
                  │ Sự cố: vLLM GPU sập / OOM    │
                  └──────────────┬───────────────┘
                                 │
                                 ▼
           ┌────────────────────────────────────────────┐
           │ Bước 1: Circuit Breaker Tự Động Failover   │
           │ - LiteLLM / ModelCircuitBreaker chuyển OPEN│
           │ - Toàn bộ traffic chuyển sang llama.cpp    │
           │ - Hệ thống tiếp tục chạy ở chế độ dự phòng│
           └─────────────────────┬──────────────────────┘
                                 │
                                 ▼
           ┌────────────────────────────────────────────┐
           │ Bước 2: Kiểm Tra Trạng Thái & Logs Cụm GPU │
           │ - Kiểm tra NVIDIA driver & VRAM            │
           │ - Đọc log tiến trình vLLM container        │
           └─────────────────────┬──────────────────────┘
                                 │
                                 ▼
           ┌────────────────────────────────────────────┐
           │ Bước 3: Khởi Động Lại vLLM Service         │
           │ - Dọn dẹp Zombie processes / GPU memory    │
           │ - Restart container / pod vLLM             │
           └─────────────────────┬──────────────────────┘
                                 │
                                 ▼
           ┌────────────────────────────────────────────┐
           │ Bước 4: Kiểm Chứng Health Check & Phục Hồi │
           │ - curl -f http://localhost:8001/health     │
           │ - Circuit Breaker tự reset về CLOSED       │
           └────────────────────────────────────────────┘
```

#### Quy Trình Thao Tác Chi Tiết Khi Có Báo Động (Alert P1):

##### Bước 1: Xác Nhận Cơ Chế Failover Hoạt Động Bình Thường
Kiểm tra log của API Server để xác nhận `ModelCircuitBreaker` đã kích hoạt chế độ dự phòng:
```bash
# Xem log phát hiện lỗi model
docker logs agentic-api --tail 100 | grep -E "Circuit breaker active|Falling back"
# Hoặc kiểm tra trạng thái HTTP trả về từ llama.cpp (Port 8080)
curl -s http://localhost:8080/v1/models | grep "llama-server"
```
*Kết quả kỳ vọng*: Request của người dùng vẫn được đáp ứng bình thường qua CPU model (tốc độ có thể chậm hơn nhưng không sinh mã lỗi 500).

##### Bước 2: Kiểm Tra Tình Trạng GPU & Nguyên Nhân Sập
Truy cập GPU Worker Node để kiểm tra lỗi phần cứng hoặc Out-Of-Memory (OOM):
```bash
# Kiểm tra tình trạng GPU và tiến trình chiếm dụng VRAM
nvidia-smi
# Kiểm tra log sự cố của container vLLM
docker logs agentic-vllm --tail 200 | grep -E "CUDA out of memory|Error|Exception"
```

##### Bước 3: Khởi Động Lại Dịch Vụ vLLM
Thực hiện dọn dẹp bộ nhớ và restart container vLLM:
```bash
# Trong môi trường Docker Compose
docker compose -f deploy/infra/docker-compose.stack.yml restart vllm

# Hoặc trong môi trường Kubernetes
kubectl rollout restart deployment/vllm -n agentic-platform
```

##### Bước 4: Kiểm Chứng & Phục Hồi Traffic
Thực hiện kiểm tra cổng sẵn sàng (Readiness Probe) trước khi tiếp nhận lưu lượng chính thức:
```bash
# Kiểm tra endpoint vLLM trả về mã HTTP 200
curl -i http://localhost:8001/v1/models

# Kiểm tra probe định tuyến qua LiteLLM
curl -i http://localhost:4000/health
```
Khi vLLM phản hồi thành công, `ModelCircuitBreaker` sẽ tự động chuyển từ trạng thái `HALF_OPEN` về `CLOSED`, đưa lưu lượng suy luận chính thức quay trở lại cụm GPU.

---

## 6. Kiến Trúc Bộ Đệm Toàn Diện (Multi-Tier Caching)

Hệ thống triển khai 3 tầng cache đồng bộ nhằm giảm tải truy vấn và tối ưu thời gian phản hồi:

1. **RAG Semantic & Query Cache (`src/agentic/cache/rag_cache.py`, `src/agentic/cache/redaction.py`)**:
   - Kiến trúc Two-Tier: Tier 1 (L1) In-Memory LRU cục bộ trong từng pod và Tier 2 (L2) Distributed Redis Cache dùng chung toàn cluster (`redis-cache` + `redis-state` invalidation counter).
   - Tự động lazy-resolve embedding neural qua `_active_embed_fn()` khi `configure_embedding_model()` chạy sau import; chặn khớp mờ bằng vector trigram ở `staging`/`production` hoặc khi `require_embedding_service=True`; đối chiếu `_quantifier_tokens` (số, ngày, phiên bản) chống cache hit nhầm ngữ cảnh số liệu; hỗ trợ chia sẻ cache an toàn theo `tenant_id`/`department`/`user_roles` (hạ về `private_scope` nếu node chỉ cấp quyền theo `owner_id`) và redact PII đồng nhất L1/L2 mà vẫn bảo toàn nguyên vẹn khối `[CITATIONS]...[/CITATIONS]`.
   - TTL mặc định 3600 giây; tự động giải phóng vùng nhớ theo chính sách LRU khi đạt ngưỡng giới hạn.
2. **Prompt & Completion Cache (`src/agentic/cache/prompt_cache.py`)**:
   - Kiến trúc Two-Tier: Tier 1 Memory kết hợp Tier 2 Distributed Redis.
   - Lưu trữ kết quả hoàn thành của các câu hỏi tất định (temperature $\le$ 0.7) dựa trên chuỗi tuần tự hóa (`SHA256(model + temp + messages)`).
   - Triệt tiêu chi phí suy luận đối với các câu hỏi trùng lặp hoặc các bước ReAct lặp lại giữa các phiên hội thoại.
3. **KV-Cache & Prefix Caching Optimizer (`src/agentic/cache/kv_cache.py`)**:
   - Tối ưu hóa bộ nhớ Key-Value của GPU trên vLLM / llama.cpp (Automatic Prefix Caching - APC).
   - **Quy tắc bất biến**: Ghim `SystemMessage` tĩnh lên đầu index 0 (không chứa tham số động như timestamp, UUID).
   - Tự động gắn header `X-Cache-Control: ephemeral` và `anthropic-beta: prompt-caching` trong mọi HTTP request gửi tới Gateway.

---

## 7. Danh Mục Tiến Trình & Bản Đồ Cổng Mạng (Port Allocation Map)

Hệ thống phân định ranh giới mạng nghiêm ngặt theo bảng cổng sau:

| Tiến Trình / Dịch Vụ | Cổng (Port) | Giao Thức | Vai Trò & Chức Năng |
| :--- | :---: | :---: | :--- |
| **API Server (L1 Interface)** | `8000` | HTTP / SSE | Cổng giao tiếp chính cho Web Client, mobile app, SSE streaming |
| **A2A ChatAgent** | `9100` | A2A / HTTP | Service tác tử đàm thoại ngữ cảnh thông thường |
| **A2A RAGAgent** | `9101` | A2A / HTTP | Service tác tử tra cứu cơ sở tri thức nội bộ |
| **A2A CodingAgent** | `9102` | A2A / HTTP | Service tác tử lập trình, sinh mã và kiểm thử |
| **MCP Code Exec Server** | `9001` | FastMCP / Streamable-HTTP (`/mcp`, stateless) | Sandbox cách ly thực thi mã nguồn Python/Bash (cắt stdout/stderr $\le 32\text{ KiB}$) |
| **MCP Repo Server** | `9002` | FastMCP / Streamable-HTTP (`/mcp`, stateless) | Thao tác git, duyệt cây thư mục, đọc symbol (chặn `data/index`, `data/raw`) |
| **MCP Web Search Server** | `9003` | FastMCP / Streamable-HTTP (`/mcp`, stateless) | Tìm kiếm thông tin mở rộng ngoài Internet (`WEB_SEARCH_API_URL`) |
| **LiteLLM Gateway** | `4000` | OpenAI API | Bộ định tuyến model, quản lý hạn ngạch, circuit breaker |
| **vLLM Inference Engine** | `8001` / `8002` | OpenAI API | Cụm GPU phục vụ Qwen2.5-72B-Instruct và Qwen2.5-Coder-32B-Instruct |
| **llama-server (Chat)** | `8080` | OpenAI API | Cụm CPU phục vụ chat dự phòng và tác vụ edge |
| **llama-server (Embed)** | `8081` | OpenAI API | Cụm CPU sinh vector embedding (`BAAI/bge-m3`) |
| **PostgreSQL Database** | `5432` | TCP | `AsyncConnectionPool` checkpoint store, LTM (`pg_advisory_xact_lock` + hash-chained `audit_logs` với `chain_seq` bất biến) |
| **Qdrant Vector DB** | `6333` | HTTP / gRPC | Vector database lưu trữ dense index cho RAG (cô lập theo `tenant_id` từ JWT, backup qua `agentic-qdrant-backup` trên PVC `agentic-qdrant-backups`) |
| **Redis Cache (`REDIS_URL`) & State (`REDIS_STATE_URL`)** | `6379` | TCP | Tách `redis-cache` (`allkeys-lru`: L2 cache, rate limit) và `redis-state` (`noeviction`: `RedisSaver`, nonces, thread ownership, JTI revocation) |
| **Neo4j Graph Database (optional, production default: disabled)** | `7474` / `7687` | HTTP / Bolt | Backend KG tùy chọn; production hiện dùng đường NetworkX/Qdrant đã kiểm định |
| **Temporal Server & Worker Probes** | `7233` / `9105` | gRPC / HTTP | Engine điều phối workflow dài hạn (Worker expose `/healthz` & `/readyz` trên `:9105`) |
