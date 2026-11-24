# AGENTS_PROTOCOLS_NETWORKING.md — Giao Thức Tương Tác & Kết Nối Mạng

> **Tài liệu tham chiếu chuyên sâu** thuộc phân vùng kiến trúc Multi-Agent trong hệ sinh thái `agentic-platform`.  
> Tài liệu gốc: [AGENTS.md](../../AGENTS.md) · [ARCHITECTURE.md](../../Architecture.md).

---

## 1. Giao Thức A2A (Agent-to-Agent)

- Sử dụng chuẩn `a2a-sdk` bọc quanh LangGraph thông qua `A2A AgentExecutor` (`src/agentic/a2a/executor.py`).
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
      "run_id": "run_uuid_5678",
      "owner_id": "tenant_abc",
      "approval_capability": "base64url_payload.hmac_signature"
    }
  }
  ```

---

## 2. Giao Thức MCP (Model Context Protocol)

> Production bắt buộc dùng MCP SSE/HTTP tới các service độc lập. Local handler chỉ được bật ở development/test. Mọi request MCP mạng mang `X-Internal-Service-Token` cùng execution identity headers; allowlist và capability HITL theo step được kiểm tra lại ngay tại invocation boundary.

- Supervisor phát capability HMAC có TTL tối đa 5 phút, khóa đồng thời theo `run_id`, `step_id`, `owner_id`, `agent_name` và danh sách tool được duyệt. Step thường vẫn nhận execution identity đã ký nhưng danh sách tool đặc quyền rỗng.
- A2A executor bỏ qua hoàn toàn cờ boolean `hitl_approved` từ request; owner/tenant chỉ được đưa vào execution context khi capability hợp lệ.
- MCP client truyền `X-Agent-Name`, `X-Step-Id`, `X-Owner-Id`, `X-Run-Id` và `X-Approval-Capability`. MCP middleware xác minh chữ ký/binding; từng wrapper tool tiếp tục kiểm tra agent allowlist và tool-specific approval.
- Istio AuthorizationPolicy giới hạn theo service account: CodingAgent chỉ tới code/repo MCP, ChatAgent chỉ tới web-search MCP; RAGAgent không được gọi MCP service ngoài allowlist.

- Các tool servers được viết bằng `FastMCP`:
  - `code_exec_server.py` (Port 9001)
  - `repo_server.py` (Port 9002)
  - `web_search_server.py` (Port 9003)
- `mcp_client.py` quản lý kết nối, tự động phát hiện tools và áp dụng **Allowlist** theo cấu hình `configs/policies.yaml`. Sub-agent nào chỉ được phép bind những tools được khai báo cho sub-agent đó.

---

## 3. Cổng Mạng & Bản Đồ Phân Phối Cổng (Port Allocation Map)

| Dịch vụ / Tiến trình | Cổng (Port) | Giao thức | Mô tả |
| :--- | :--- | :--- | :--- |
| **API Server (L1 Interface)** | `8000` | HTTP / SSE | Endpoint chính cho Web UI, Client, Webhooks |
| **MCP Code Exec Server** | `9001` | FastMCP / HTTP | Sandbox thực thi code |
| **MCP Repo Server** | `9002` | FastMCP / HTTP | Thao tác git / file / symbol |
| **MCP Web Search Server** | `9003` | FastMCP / HTTP | Tìm kiếm thông tin ngoài web |
| **A2A ChatAgent** | `9100` | A2A / HTTP | Service của ChatAgent |
| **A2A RAGAgent** | `9101` | A2A / HTTP | Service của RAGAgent |
| **A2A CodingAgent** | `9102` | A2A / HTTP | Service của CodingAgent |
| **LiteLLM Gateway** | `4000` | OpenAI API | Bộ định tuyến model, quota, fallback |
| **vLLM Engine** | `8001` | OpenAI API | High-throughput GPU inference (Qwen3-72B) |
| **llama-server (Chat)** | `8080` | OpenAI API | CPU/Edge GGUF chat inference |
| **llama-server (Embed)** | `8081` | OpenAI API | BGE Embedding generation service |
| **Postgres** | `5432` | TCP | Checkpoint store, long-term memory |
| **Qdrant Vector DB** | `6333` | HTTP / gRPC | Vector collection cho RAG |
| **Temporal Server** | `7233` | gRPC | Durable execution engine cho workflows |

---

## 4. Kiến Trúc Bộ Đệm Toàn Diện (Caching Architecture: RAG, Prompt & KV Cache)

Hệ thống triển khai kiến trúc bộ đệm đa tầng (Multi-Tier Caching) tối ưu hóa thông lượng và chi phí:
1. **RAG Semantic & Query Cache (`src/agentic/cache/rag_cache.py`)**:
   - Kiến trúc Two-Tier: Tier 1 (L1) in-memory LRU cục bộ trong Pod và Tier 2 (L2) Distributed Redis cache dùng chung cho toàn bộ cluster Kubernetes.
   - Cache các kết quả tìm kiếm tri thức và reranking theo mã hash chuẩn hóa của query (`SHA256(normalized_query + top_k)`).
   - Tự động kiểm tra trước khi query vector store / cross-encoder. Thời gian sống (TTL) mặc định 3600s, cơ chế đẩy LRU khi vượt quá `cache_max_entries`.
   - Giảm tải tới 70-80% truy vấn trùng lặp vào Vector Store.
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

- **Kiểm định kiến trúc phân tầng & ranh giới giao thức**: [REVIEW_ARCHITECTURE & PROTOCOLS.md](../../.workflows/reviews/REVIEW_ARCHITECTURE%20&%20PROTOCOLS.md)
- **Kiểm định hệ thống bộ đệm đa tầng**: [REVIEW_MULTI-TIER CACHING SUBSYSTEM.md](../../.workflows/reviews/REVIEW_MULTI-TIER%20CACHING%20SUBSYSTEM.md)
- **Kiểm định cổng mô hình & phục vụ suy luận**: [REVIEW_MODEL GATEWAY & INFERENCE.md](../../.workflows/reviews/REVIEW_MODEL%20GATEWAY%20&%20INFERENCE.md)
- **Kiểm định hạ tầng mạng Kubernetes & mTLS**: [REVIEW_KUBERNETES & SRE.md](../../.workflows/reviews/REVIEW_KUBERNETES%20&%20SRE.md)
