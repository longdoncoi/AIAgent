# AGENTS_AGENT_SPECIFICATIONS.md — Đặc Tả Chi Tiết Từng Agent

> **Tài liệu tham chiếu chuyên sâu** thuộc phân vùng kiến trúc Multi-Agent trong hệ sinh thái `agentic-platform`.  
> Tài liệu gốc: [AGENTS.md](../../AGENTS.md) · [ARCHITECTURE.md](../../Architecture.md).

---

## 1. L2 Supervisor Orchestrator

- **Thư mục mã nguồn**: `src/agentic/orchestrator/`
- **Prompt cấu hình**: `configs/prompts/supervisor.md`, `configs/prompts/planner.md`, `configs/prompts/reflector.md`
- **State Schema (`state.py`)**:
  ```python
  from typing import Annotated, Literal
  from pydantic import BaseModel, Field
  from langgraph.graph.message import add_messages

  class Step(BaseModel):
      id: str
      description: str
      agent: Literal["chat", "rag", "coding"]
      input_query: str
      status: Literal["pending", "running", "completed", "failed"] = "pending"
      output: str | None = None
      risk_level: Literal["low", "medium", "high"] = "low"

  class Plan(BaseModel):
      goal: str
      steps: list[Step] = Field(default_factory=list)

  class AgentState(BaseModel):
      messages: Annotated[list, add_messages]
      plan: Plan | None = None
      current_step_index: int = 0
      reflection_notes: list[str] = Field(default_factory=list)
      requires_approval: bool = False
      final_answer: str | None = None
  ```
- **Luồng Thực Thi của Graph (`graph.py`)**:
  1. `planner`: Nhận request, sinh `Plan`. Nếu task đơn giản, gán trực tiếp 1 step.
  2. `dispatch`: Phân tích các step có thể chạy song song (fan-out) hoặc tuần tự.
  3. `hitl`: Kiểm tra `risk_level`. Nếu `risk_level == "high"` (ví dụ lệnh xóa dữ liệu, execute bash nhạy cảm), gọi `interrupt()` dừng graph, lưu checkpoint và chờ tín hiệu qua `/approvals`.
  4. `run_step`: Gửi payload qua A2A Client tới Sub-Agent tương ứng.
  5. `reflector`: Kiểm tra đầu ra từ các Sub-Agent, đối chiếu yêu cầu ban đầu.
     - *Pass*: Chuyển sang `finalize`.
     - *Fail*: Cập nhật `reflection_notes`, re-plan hoặc retry step.
  6. `finalize`: Tổng hợp câu trả lời cuối cùng, stream về L1 API qua SSE.

---

## 2. L3 Sub-Agent: ChatAgent

- **Thư mục**: `src/agentic/agents/chat/` (`graph.py`, `server.py`)
- **A2A Endpoint**: Port `9100` (`http://localhost:9100/a2a`)
- **AgentCard Definition (`src/agentic/a2a/cards.py`)**:
  - `name`: `"chat_agent"`
  - `description`: "Chuyên xử lý đàm thoại thông thường, tóm tắt nội dung, giải thích khái niệm, định hình câu hỏi."
  - `skills`: `["general_chit_chat", "text_summarization", "query_disambiguation"]`
- **Cơ chế hoạt động**:
  - Khởi tạo ReAct loop chuẩn (`build_react_graph()`).
  - Giao tiếp nhanh, ưu tiên độ trễ thấp (low latency) qua alias `fast-llm`.
  - Không có quyền gọi các tool thay đổi trạng thái hệ thống.

---

## 3. L3 Sub-Agent: RAGAgent

- **Thư mục**: `src/agentic/agents/rag/` (`graph.py`, `server.py`)
- **A2A Endpoint**: Port `9101` (`http://localhost:9101/a2a`)
- **AgentCard Definition**:
  - `name`: `"rag_agent"`
  - `description`: "Chuyên truy vấn và giải đáp dựa trên cơ sở tri thức nội bộ, tài liệu quy định, chính sách, tài liệu kỹ thuật."
  - `skills`: `["enterprise_knowledge_search", "policy_lookup", "grounded_qa"]`
- **Kiến trúc RAG LlamaIndex (`src/agentic/rag/`)**:
  - **Ingestion (`ingestion.py`)**: Hierarchical Node Parser (parent chunk 1024 tokens, child chunk 256 tokens).
  - **Retriever (`retriever.py`)**: Hybrid Search = BM25 (lexical) + Dense Embedding (Qdrant/FAISS) thông qua Reciprocal Rank Fusion (RRF). Kỹ thuật Auto-merging retrieval để lấy context đầy đủ từ parent chunk khi nhiều child chunk khớp.
  - **Reranker (`reranker.py`)**: Sử dụng Cross-Encoder (ví dụ `bge-reranker-large`) để chấm lại điểm phù hợp trước khi đưa vào context window.
  - **Tool binding**: Cung cấp hàm `search_policy(query: str, filters: dict)` cho LangGraph ReAct loop của RAGAgent.

---

## 4. L3 Sub-Agent: CodingAgent

- **Thư mục**: `src/agentic/agents/coding/` (`graph.py`, `server.py`)
- **A2A Endpoint**: Port `9102` (`http://localhost:9102/a2a`)
- **AgentCard Definition**:
  - `name`: `"coding_agent"`
  - `description`: "Chuyên phân tích cấu trúc mã nguồn, viết mã, chạy script kiểm thử trong sandbox và gỡ lỗi (debug)."
  - `skills`: `["code_generation", "code_review", "sandbox_execution", "git_repo_inspection"]`
- **Kết nối MCP Tools**:
  - `code_exec_server` (Port 9001): Thực thi mã Python/Bash trong Docker/gVisor sandbox cô lập. Có timeout cứng và giới hạn RAM/CPU.
  - `repo_server` (Port 9002): Tìm kiếm symbol, đọc file, diff git, liệt kê cây thư mục.
- **Ràng buộc an toàn**: Mọi thao tác ghi đè file dự án hoặc chạy lệnh bash nguy hiểm bắt buộc phải gán `risk_level: high` trong metadata để Supervisor kích hoạt HITL.

---

## 5. Tài Liệu Kiểm Định & Tham Chiếu Liên Quan

- **Kiểm định kiến trúc và giao thức A2A**: [REVIEW_ARCHITECTURE & PROTOCOLS.md](../../.workflows/reviews/REVIEW_ARCHITECTURE%20&%20PROTOCOLS.md)
- **Kiểm định chính sách bảo mật và HITL**: [REVIEW_SECURITY, GUARDRAILS & HITL.md](../../.workflows/reviews/REVIEW_SECURITY,%20GUARDRAILS%20&%20HITL.md)
- **Kiểm định chất lượng mô hình & RAG**: [REVIEW_QUALITY GATES - DEEPEVAL & RAGAS.md](../../.workflows/reviews/REVIEW_QUALITY%20GATES%20-%20DEEPEVAL%20&%20RAGAS.md)
