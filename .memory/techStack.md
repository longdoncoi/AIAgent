# techStack.md — Tổng Hợp Công Nghệ, Phiên Bản & Cổng Dịch Vụ

> Tệp này tổng hợp toàn bộ các thông số kỹ thuật, phiên bản runtime, thư viện và quy hoạch cổng mạng của hệ sinh thái `agentic-platform`.

---

## 1. Môi Trường Runtime & Core Libraries

- **Ngôn ngữ**: Python 3.12+
- **Quản lý gói & Virtualenv**: `uv`
- **Web Framework (L1 API)**: FastAPI + Uvicorn
- **Orchestration Framework (L2)**: LangGraph (StateGraph, ReAct loop, Interruptible HITL)
- **Tool Protocol (L4)**: FastMCP (Model Context Protocol)
- **Knowledge Engine (L5)**: LlamaIndex (Hierarchical Chunking, Auto-merging retrieval, Hybrid Search, Cross-Encoder reranker)
- **Memory & State (L6)**: LangGraph PostgresSaver / RedisSaver (HMAC-SHA256 authenticated)
- **Model Gateway (L7)**: LiteLLM (OpenAI-compatible unified routing)
- **Inference Engines**: vLLM (GPU / Port 8001), llama.cpp (CPU / Ports 8080, 8081)
- **Testing & Quality Gates**: Pytest, Ruff, Mypy, DeepEval, RAGAS Framework
- **Observability (L9)**: OpenTelemetry, Jaeger, Prometheus, LangSmith

---

## 2. Bảng Phân Bổ Cổng Mạng (Port Allocation)

| Dịch vụ / Tiến trình | Cổng (Port) | Giao thức | Mô tả |
| :--- | :--- | :--- | :--- |
| **L1 Interface API** | `8000` | HTTP / SSE | REST API & Server-Sent Events cho Client/UI |
| **MCP Code Exec Server** | `9001` | FastMCP / HTTP | Sandbox thực thi code Python/Bash |
| **MCP Repo Server** | `9002` | FastMCP / HTTP | Thao tác git / file / symbol |
| **MCP Web Search Server**| `9003` | FastMCP / HTTP | Tìm kiếm web bên ngoài |
| **A2A ChatAgent** | `9100` | A2A / HTTP | Đàm thoại ngữ cảnh thông thường |
| **A2A RAGAgent** | `9101` | A2A / HTTP | Truy xuất tài liệu & chính sách nội bộ |
| **A2A CodingAgent** | `9102` | A2A / HTTP | Đọc mã nguồn, sinh code, gỡ lỗi sandbox |
| **LiteLLM Gateway** | `4000` | HTTP / OpenAI | Cổng điều phối mô hình |
| **vLLM Engine** | `8001` | HTTP / OpenAI | High-throughput GPU inference |
| **llama-server (Chat)**| `8080` | HTTP / OpenAI | CPU/Edge GGUF chat inference |
| **llama-server (Embed)**| `8081` | HTTP / OpenAI | BGE Embedding generation service |
| **PostgreSQL** | `5432` | TCP | Checkpoint store, long-term memory |
| **Qdrant Vector DB** | `6333` | HTTP / gRPC | Vector collection cho RAG |
| **Temporal Server** | `7233` | gRPC | Durable execution engine cho workflows |
