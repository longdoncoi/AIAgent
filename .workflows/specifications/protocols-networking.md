# AGENTS_PROTOCOLS_NETWORKING.md — Giao Thức Tương Tác & Kết Nối Mạng

> **Tài liệu tham chiếu chuyên sâu** thuộc phân vùng kiến trúc Multi-Agent trong hệ sinh thái `agentic-platform`.  
> Tài liệu gốc: [AGENTS.md](../../AGENTS.md) · [Architecture.md](../../Architecture.md).

---

## 1. Giao Thức A2A (Agent-to-Agent)

- Hiện triển khai **A2A-inspired REST/SSE profile nội bộ**, không tuyên bố tương thích JSON-RPC A2A SDK: `A2AAgentExecutor` phục vụ `/a2a/tasks` và `/a2a/tasks/stream`, kèm `AgentCard` và envelope Ed25519 (`src/agentic/a2a/executor.py`).
- **Giao thức**: JSON over HTTP/gRPC.
- **Cấu trúc gói tin A2A Task**:
  ```json
  {
    "task_id": "step_uuid_1234",
    "caller": "supervisor",
    "target_agent": "coding_agent",
    "payload": {
      "instruction": "Chạy unit test module auth và báo cáo lỗi",
      "context": { "repo_path": "/workspace" }
    },
    "metadata": {
      "trace_id": "otel_4567",
      "timeout_ms": 30000,
      "tenant_id": "org-acme",
      "owner_id": "alice",
      "thread_id": "thread-1",
      "turn_id": "turn-abcd",
      "idempotency_key": "sha256(step_id:agent:tenant:owner:thread:turn:fp)"
    }
  }
  ```
- **Idempotency & Streaming Retry**: Cả client (`hitl.py` / `dispatch.py` / `client.py`) và server (`executor.py` qua `_scoped_idempotency_key`) đều gắn khóa phạm vi theo `tenant_id`, `owner_id`, `thread_id`, `turn_id` và `step_fingerprint` trước khi đọc/ghi `agentic:a2a:idem:*` trên Redis (`redis-state`, có fallback local ở môi trường non-prod), triệt tiêu rò rỉ cache chéo tenant. `call_agent_stream` chỉ retry trước khi nhận SSE frame đầu tiên và tuân thủ `allow_local_agent_fallback`.
- **S2S Authentication**: Hỗ trợ danh sách token luân phiên (`s2s_accepted_tokens` ngoài token chính) và truyền `tenant_id` từ JWT xuyên suốt `get_current_user` $\rightarrow$ `AgentState.metadata` $\rightarrow$ A2A metadata.

---

## 2. Giao Thức MCP (Model Context Protocol)

> Production bắt buộc dùng MCP **Streamable-HTTP** chế độ **stateless** (`/mcp`, `stateless_http=True, json_response=True`) tới các service độc lập (Kubernetes Service có `sessionAffinity: ClientIP` và `preStop` graceful shutdown). Local handler chỉ được bật ở development/test. Mọi request MCP mạng mang token `X-Internal-Service-Token`; allowlist, default-deny cho tool chưa khai báo, và capability HITL theo step/tool-call được kiểm tra lại ngay tại invocation boundary.

- Các tool servers được triển khai bằng `MCPServer` (qua `run_mcp_server` mặc định `MCP_TRANSPORT="streamable-http"` trên `/mcp` và hỗ trợ fallback `sse` khi cấu hình tường minh):
  - `code_exec_server.py` (Port 9001, cắt stdout/stderr tối đa 32 KiB)
  - `repo_server.py` (Port 9002, chặn đọc `data/index`, `data/raw` và ngăn regex ReDoS)
  - `web_search_server.py` (Port 9003, hỗ trợ backend HTTP cấu hình qua `WEB_SEARCH_API_URL`)
- `mcp_client.py` quản lý kết nối qua `streamable_http_client` (sử dụng `httpx2.AsyncClient` tương thích `mcp>=2.0.0`, khai báo tường minh trong `pyproject.toml`), tự động phát hiện tools, áp dụng **Allowlist** + timeout từng tool (`policy.get_tool_timeout`) theo `configs/policies.yaml` (giữ bản cấu hình hợp lệ trước đó khi hot-reload gặp lỗi YAML). Sub-agent nào chỉ được phép bind những tools được khai báo cho sub-agent đó.
- **Kustomize Self-Sufficiency**: Base Kubernetes (`deploy/k8s/kustomization.yaml`) gồm đầy đủ cả ứng dụng lẫn hạ tầng cốt lõi qua `serving.yaml` (`vllm`, `embedding`, `litellm`) và `datastores.yaml` (`postgres`, `redis-cache`, `redis-state`, `qdrant`, `temporal`).

---

## 3. Cổng Mạng & Bản Đồ Phân Phối Cổng (Port Allocation Map)

| Dịch vụ / Tiến trình | Cổng (Port) | Giao thức | Mô tả |
| :--- | :--- | :--- | :--- |
| **API Server (L1 Interface)** | `8000` | HTTP / SSE | Endpoint chính cho Web UI, Client, Webhooks |
| **MCP Code Exec Server** | `9001` | MCP Streamable-HTTP (`/mcp`) | Sandbox thực thi code |
| **MCP Repo Server** | `9002` | MCP Streamable-HTTP (`/mcp`) | Thao tác git / file / symbol |
| **MCP Web Search Server** | `9003` | MCP Streamable-HTTP (`/mcp`) | Tìm kiếm thông tin ngoài web |
| **A2A ChatAgent** | `9100` | A2A / HTTP | Service của ChatAgent |
| **A2A RAGAgent** | `9101` | A2A / HTTP | Service của RAGAgent |
| **A2A CodingAgent** | `9102` | A2A / HTTP | Service của CodingAgent |
| **LiteLLM Gateway** | `4000` | OpenAI API | Bộ định tuyến model, quota, fallback |
| **vLLM Engine** | `8001` | OpenAI API | High-throughput GPU inference (Qwen2.5-72B-Instruct) |
| **llama-server (Chat)** | `8080` | OpenAI API | CPU/Edge GGUF chat inference |
| **llama-server (Embed)** | `8081` | OpenAI API | BGE Embedding generation service |
| **Postgres** | `5432` | TCP | Checkpoint store, long-term memory |
| **Qdrant Vector DB** | `6333` | HTTP / gRPC | Vector collection cho RAG |
| **Temporal Server** | `7233` | gRPC | Durable execution engine cho workflows |
| **Temporal Worker Probes** | `9105` | HTTP | Probe `/healthz` và `/readyz` của worker (`TEMPORAL_WORKER_HEALTH_PORT`, hỗ trợ graceful shutdown SIGTERM) |

---

## 4. Kiến Trúc Bộ Đệm Toàn Diện (Caching Architecture: RAG, Prompt & KV Cache)

Hệ thống triển khai kiến trúc bộ đệm đa tầng (Multi-Tier Caching) tối ưu hóa thông lượng và chi phí:
1. **RAG Semantic & Query Cache (`src/agentic/cache/rag_cache.py`, `src/agentic/cache/redaction.py`)**:
   - Kiến trúc Two-Tier: Tier 1 (L1) in-memory LRU cục bộ trong Pod và Tier 2 (L2) Distributed Redis cache (`redis-cache`, kết hợp invalidation counter trên `redis-state`).
   - Tự động lazy-resolve `LlamaSettings.embed_model` qua `_active_embed_fn()` khi `configure_embedding_model()` chạy sau import; chặn khớp mờ bằng trigram hash trên `staging`/`production` hoặc khi `require_embedding_service=True`; kiểm tra `_quantifier_tokens` (số, ngày, phiên bản) chống match nhầm câu khác số liệu.
   - Hỗ trợ chia sẻ cache chéo người dùng trong cùng `tenant_id`/`department`/`user_roles` (tự động hạ về `private_scope` nếu bất kỳ document nào yêu cầu `owner_id`-only ACL) và redact PII đồng nhất cả L1 lẫn L2 mà vẫn bảo toàn nguyên khối `[CITATIONS]...[/CITATIONS]`. Thời gian sống (TTL) mặc định 3600s, cơ chế đẩy LRU khi vượt quá `cache_max_entries`.
2. **Prompt & Completion Cache (`src/agentic/cache/prompt_cache.py`)**:
   - Kiến trúc Two-Tier: L1 in-memory kết hợp L2 Distributed Redis cache.
   - Cache các phản hồi LLM với các request mang tính tất định (temperature <= 0.7) dựa trên chuỗi tuần tự hóa của tin nhắn (`SHA256(model + temp + messages)`).
   - Tránh việc gọi lặp lại LLM cho các bước suy luận trùng lặp giữa các lượt đàm thoại.
3. **KV-Cache & Prefix Caching Optimizer (`src/agentic/cache/kv_cache.py`)**:
   - Tối ưu hóa bộ nhớ Key-Value của GPU trên vLLM / llama.cpp / SGLang (Automatic Prefix Caching - APC).
   - **Quy tắc bất biến**: Ghim `SystemMessage` tĩnh lên đầu index 0 không chứa dữ liệu động (timestamp, random uuid). Dữ liệu động đặt hoàn toàn ở các `HumanMessage` phía sau.
   - Gắn header `X-Cache-Control: ephemeral` và `anthropic-beta: prompt-caching` để router LiteLLM và vLLM tận dụng tối đa cache block GPU.

---

## 5. Cơ Chế Quản Lý Kết Nối & Tối Ưu Hóa Tải Cao (High-Concurrency Networking & Zero-Trust)

1. **A2A HTTP Connection Pooling (`src/agentic/a2a/client.py`)**:
   - Duy trì singleton `httpx.AsyncClient` tái sử dụng connection pool (`max_keepalive_connections=50, max_connections=200, keepalive_expiry=30.0`), triệt tiêu chi phí bắt tay TCP/SSL và chống kiệt socket khi chịu tải 1000+ RPS.
   - Quản lý đóng kết nối duyên dáng (`aclose()`) khi pod dừng (SIGTERM).
2. **Zero-Trust CORS Origin Validation (`src/agentic/core/config.py`, `src/agentic/api/main.py`)**:
   - Khai báo danh sách domain nguồn cụ thể (`allowed_origins`). Ràng buộc bất biến: Nghiêm cấm wildcard `*` trên môi trường Production khi bật `allow_credentials: true`.
3. **Non-blocking Async RAG Offloading (`src/agentic/rag/tools.py`)**:
   - Tách biệt luồng xử lý CPU/Disk I/O của LlamaIndex / BM25 / Cross-Encoder thông qua `asyncio.to_thread(search_policy, ...)`. Giữ cho event loop chính của FastAPI luôn phản hồi tức thời dưới tải cao.

---

## 6. Tài Liệu Kiểm Định & Tham Chiếu Liên Quan

- **Kiểm định kiến trúc phân tầng & ranh giới giao thức**: [REVIEW_ARCHITECTURE & PROTOCOLS.md](../reviews/REVIEW_ARCHITECTURE%20&%20PROTOCOLS.md)
- **Kiểm định hệ thống bộ đệm đa tầng**: [REVIEW_MULTI-TIER CACHING SUBSYSTEM.md](../reviews/REVIEW_MULTI-TIER%20CACHING%20SUBSYSTEM.md)
- **Kiểm định cổng mô hình & phục vụ suy luận**: [REVIEW_MODEL GATEWAY & INFERENCE.md](../reviews/REVIEW_MODEL%20GATEWAY%20&%20INFERENCE.md)
- **Kiểm định hạ tầng mạng Kubernetes & mTLS**: [REVIEW_KUBERNETES & SRE.md](../reviews/REVIEW_KUBERNETES%20&%20SRE.md)
