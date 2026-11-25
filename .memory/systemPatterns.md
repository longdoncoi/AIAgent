# systemPatterns.md — Các Mẫu Thiết Kế Chuẩn Hóa & Quy Ước Bất Biến

> Tệp này ghi chép các quy ước lập trình, mẫu kiến trúc (patterns) chuẩn hóa của codebase `agentic-platform`.

---

## 1. Mẫu Thiết Kế L2 LangGraph State & DAG
- Mọi node trong LangGraph Supervisor nhận `AgentState` và trả về dict update tương ứng.
- Phải duy trì danh sách message bất biến thông qua `Annotated[list, add_messages]`.
- Logic điều phối song song (fan-out) sử dụng danh sách `Step` trong `Plan`, chỉ những step độc lập dữ liệu mới được dispatch cùng lúc.

---

## 2. Mẫu Xử Lý A2A (Agent-to-Agent Protocol)
- Sử dụng singleton `httpx.AsyncClient` với connection pool (`max_keepalive_connections=50, max_connections=200`).
- Mọi request A2A phải mang `trace_id` trong header metadata để liên kết OpenTelemetry trace.
- Xử lý timeout chuẩn hóa:
  ```python
  try:
      response = await a2a_client.dispatch_task(task_payload, timeout=30.0)
  except httpx.TimeoutException:
      raise AgentTimeoutError(f"Sub-agent {target} timed out after 30s")
  ```

---

## 3. Mẫu FastMCP Tool Server
- Mọi tool triển khai bằng decorator `@mcp.tool()` của FastMCP.
- Tool trả về chuỗi JSON string hoặc văn bản đã định dạng rõ ràng, không raise uncaught exception ra ngoài mà bọc thành `AgentError`.

---

## 4. Mẫu Caching Đa Tầng (Two-Tier LRU + Redis)
- Kiểm tra Tier 1 (In-Memory LRU) trước. Nếu miss, kiểm tra Tier 2 (Redis).
- Key luôn được hash chuẩn hóa:
  - RAG Cache: `SHA256(normalized_query + top_k)`
  - Prompt Cache: `SHA256(model + temperature + messages)`
- TTL mặc định 3600s, cơ chế dọn dẹp LRU tự động khi vượt quá dung lượng.

---

## 5. Quy Ước Tách Biệt CPU-Bound & Non-blocking Event Loop
- Các tác vụ tính toán nặng (LlamaIndex ingestion, BM25 ranking, Cross-encoder inference) không được chạy trực tiếp trên event loop của FastAPI.
- Bắt buộc dùng `await asyncio.to_thread(sync_heavy_function, ...)`.
