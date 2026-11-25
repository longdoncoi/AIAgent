# coding-workflow.md — Quy Trình & Quy Chuẩn Lập Trình Cho AI & Kỹ Sư

> **Quy trình lập trình chuẩn** trong hệ sinh thái `agentic-platform`.  
> Tham chiếu: [Architecture.md](../Architecture.md) · [agent-instructions.md](../.context/agent-instructions.md) · [.skills/](../.skills).

---

## 1. Chuẩn Viết Code Bắt Buộc

- **Môi trường & Ngôn ngữ**: Python 3.12+ quản lý dependencies qua `uv`.
- **Type Annotations**: Bắt buộc gắn type hints 100% cho mọi tham số và giá trị trả về của hàm (`mypy --strict` compliant).
- **Schema Validation**: Sử dụng `Pydantic v2` cho toàn bộ DTO, State, Request/Response payload, Model Configuration.
- **Bất đồng bộ (Async-First)**: Mọi thao tác I/O, network call (A2A), MCP tool call, database query, vector search đều phải sử dụng `async` / `await`. Tuyệt đối không block event loop bằng các hàm blocking đồng bộ.
- **Dependency Rule**: Tuân thủ nghiêm ngặt thứ tự phân tầng kiến trúc:
  - Tầng trên (L1 API $\rightarrow$ L2 Supervisor $\rightarrow$ L3 Sub-Agents $\rightarrow$ L4/L5 Tools/RAG $\rightarrow$ L6 Memory/L7 Gateway).
  - Tầng dưới tuyệt đối không import ngược tầng trên. Thư mục `src/agentic/core/` là kernel độc lập.

---

## 2. Quy Tắc Thêm Mới Một Sub-Agent (L3)

Khi phát triển thêm Sub-Agent mới (chi tiết tại [.skills/skill-add-subagent.md](../.skills/skill-add-subagent.md)):
1. Tạo thư mục `src/agentic/agents/<tên_agent>/`.
2. Tạo `graph.py` kế thừa từ `build_react_graph()` tại `src/agentic/agents/base.py`.
3. Định nghĩa `AgentCard` tại `src/agentic/a2a/cards.py` với danh sách kỹ năng cụ thể.
4. Tạo `server.py` bọc graph bằng `A2A AgentExecutor` và cấp phát một Port chưa sử dụng (dải `910x`).
5. Khai báo Sub-Agent mới vào `configs/prompts/planner.md` và `src/agentic/orchestrator/nodes/dispatch.py`.
6. Viết integration test tương ứng tại `tests/integration/test_<tên_agent>.py`.

---

## 3. Quy Tắc Thêm Mới Một MCP Tool (L4)

Khi phát triển thêm MCP Tool mới (chi tiết tại [.skills/skill-add-mcp-tool.md](../.skills/skill-add-mcp-tool.md)):
1. Triển khai tool trong `src/agentic/tools/servers/` bằng thư viện `fastmcp`.
2. Khai báo schema và gán phân loại mức độ rủi ro (`risk_level: low | medium | high`) trong `configs/policies.yaml`.
3. Cập nhật `mcp_client.py` để tool được mount chính xác vào Sub-Agent có quyền truy cập.
4. Đảm bảo tool có timeout cứng và bọc bắt lỗi chuẩn hóa thành `AgentError`.

---

## 4. Các Lệnh Vận Hành Dự Án (Makefile Commands)

```bash
# Cài đặt môi trường và dependencies
make install

# Khởi chạy hạ tầng phụ trợ (Docker: Postgres, Qdrant, Redis, Neo4j, LiteLLM)
make infra-up

# Chạy cụm Model Serving nội bộ
make serve-vllm        # vLLM GPU worker (Port 8001)
make serve-llama-chat  # llama.cpp chat (Port 8080)
make serve-llama-embed # llama.cpp embedding (Port 8081)

# Khởi chạy các Agents và MCP Servers (chế độ phát triển)
make serve-mcp         # 3 MCP servers (9001, 9002, 9003)
make serve-agents      # 3 A2A sub-agents (9100, 9101, 9102)
make dev               # L1 FastAPI interface server (Port 8000)

# Kiểm thử & Đánh giá chất lượng
make lint              # Kiểm tra code style (ruff, mypy)
make test              # Chạy unit & integration tests
make eval              # Chạy bộ đánh giá LLM-judge
make eval-deep         # Chạy bộ benchmark DeepEval
make eval-ragas        # Chạy bộ benchmark RAGAS
make ingest            # Đọc tài liệu và lập chỉ mục RAG
```
