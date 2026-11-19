# AGENTS_DEVELOPER_GUIDELINES.md — Hướng Dẫn Phát Triển Cho AI Coding Agent & Kỹ Sư

> **Tài liệu tham chiếu chuyên sâu** thuộc phân vùng kiến trúc Multi-Agent trong hệ sinh thái `agentic-platform`.  
> Tài liệu gốc: [AGENTS.md](file:///f:/PROJECTS/AIAgent/AGENTS.md) · [Architecture.md](file:///f:/PROJECTS/AIAgent/Architecture.md).

---

Khi bạn (AI Coding Agent hoặc Developer) thực hiện sửa đổi hoặc bổ sung tính năng vào kho mã nguồn này, bạn **PHẢI TUÂN THỦ NGHIÊM NGẶT** các điều sau:

## 1. Chuẩn Viết Code

- **Ngôn ngữ**: Python 3.12+.
- **Type Annotations**: Bắt buộc gắn type hints 100% cho tham số và giá trị trả về của hàm.
- **Schema Validation**: Sử dụng `Pydantic v2` cho toàn bộ DTO, State, Request/Response payload.
- **Bất đồng bộ (Async first)**: Tất cả I/O, network call, database query, A2A call, MCP tool call phải dùng `async` / `await`.

---

## 2. Quy Tắc Thêm Mới Một Sub-Agent

1. Tạo thư mục con trong `src/agentic/agents/<tên_agent>/`.
2. Tạo `graph.py` kế thừa từ `build_react_graph()` tại `src/agentic/agents/base.py`.
3. Định nghĩa `AgentCard` tại `src/agentic/a2a/cards.py` với danh sách kỹ năng cụ thể.
4. Tạo `server.py` bọc graph bằng `A2A AgentExecutor` và cấp phát một Port chưa sử dụng (dải 910x).
5. Khai báo Sub-Agent mới vào `configs/prompts/planner.md` và `src/agentic/orchestrator/nodes/dispatch.py` để Supervisor biết cách điều phối.
6. Viết integration test tương ứng tại `tests/integration/test_<tên_agent>.py`.

---

## 3. Quy Tắc Thêm Mới Một MCP Tool

1. Triển khai tool trong `src/agentic/tools/servers/` bằng thư viện `fastmcp`.
2. Khai báo tool, mô tả schema, và gán phân loại mức độ rủi ro (`risk_level: low | medium | high`) trong `configs/policies.yaml`.
3. Cập nhật `mcp_client.py` để tool được mount chính xác vào Sub-Agent có quyền truy cập.
4. Đảm bảo tool có khả năng timeout và bắt lỗi chuẩn hóa thành `AgentError`.

---

## 4. Các Lệnh Điều Hành Dự Án (Makefile Commands)

```bash
# Cài đặt môi trường và dependencies
make install

# Khởi chạy toàn bộ hạ tầng phụ trợ (Docker: Postgres, Qdrant, Temporal, LiteLLM)
make infra-up

# Chạy cụm Model Serving nội bộ
make serve-vllm       # Khởi động vLLM GPU worker
make serve-llama-chat # Khởi động llama.cpp chat
make serve-llama-embed# Khởi động llama.cpp embedding

# Khởi chạy các Agents và MCP Servers (chế độ phát triển)
make serve-mcp        # Chạy 3 MCP servers (9001, 9002, 9003)
make serve-agents     # Chạy 3 A2A sub-agents (9100, 9101, 9102)
make dev              # Chạy L1 FastAPI interface server (Port 8000)

# Kiểm thử và Đánh giá chất lượng
make lint             # Kiểm tra code style (ruff, mypy)
make test             # Chạy unit & integration tests
make eval             # Chạy bộ đánh giá LLM-judge trong evals/
make eval-deep        # Chạy bộ đánh giá DeepEval benchmark
make eval-ragas       # Chạy bộ đánh giá RAGAS framework (Faithfulness, Relevance, Precision, Recall)
make ingest           # Đọc tài liệu từ data/raw/ và lập chỉ mục vào data/index/
```

---

## 5. Tài Liệu Kiểm Định & Tham Chiếu Liên Quan

- **Kiểm định quy trình CI/CD & GitOps**: [REVIEW_CI-CD & GITOPS.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_CI-CD%20&%20GITOPS.md)
- **Kiểm định chiến lược kiểm thử toàn diện & QA**: [REVIEW_TESTING & QA STRATEGY.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_TESTING%20&%20QA%20STRATEGY.md)
- **Kiểm định kiến trúc phân tầng & ranh giới giao thức**: [REVIEW_ARCHITECTURE & PROTOCOLS.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_ARCHITECTURE%20&%20PROTOCOLS.md)

---

*Tài liệu này được bảo trì đồng bộ cùng kiến trúc hệ thống. Mọi thay đổi về phân tầng hoặc giao thức mạng bắt buộc phải cập nhật lại tài liệu này trước khi tạo Pull Request.*
