# AGENTS_AGENT_SPECIFICATIONS.md — Đặc Tả Chi Tiết Từng Agent

> **Tài liệu tham chiếu chuyên sâu** thuộc phân vùng kiến trúc Multi-Agent trong hệ sinh thái `agentic-platform`.  
> Tài liệu gốc: [AGENTS.md](../../AGENTS.md) · [Architecture.md](../../Architecture.md).

---

## 1. L2 Supervisor Orchestrator

- **Thư mục mã nguồn**: `src/agentic/orchestrator/`
- **Prompt cấu hình**: Runtime nạp `configs/prompts/planner.md` (`orchestrator/nodes/planner.py`) và `chat_agent.md` / `rag_agent.md` / `coding_agent.md` (`agents/*/graph.py`); `configs/prompts/supervisor.md` và `configs/prompts/reflector.md` là tài liệu/template tham chiếu.
- **State Schema (`state.py`)**:
  ```python
  from typing import Annotated, Any, Literal
  from pydantic import BaseModel, Field
  from langgraph.graph.message import add_messages

  class Step(BaseModel):
      id: str
      description: str
      agent: Literal["chat", "rag", "coding"]
      input_query: str
      status: Literal["pending", "running", "completed", "failed", "interrupted"] = "pending"
      output: str | None = None
      risk_level: Literal["low", "medium", "high"] = "low"
      depends_on: list[str] = Field(default_factory=list)

  class Plan(BaseModel):
      goal: str
      steps: list[Step] = Field(default_factory=list)

  class AgentState(BaseModel):
      messages: Annotated[list, add_messages]
      plan: Plan | None = None
      current_step_index: int = 0
      reflection_notes: list[str] = Field(default_factory=list)
      reflection_count: int = 0
      degraded: bool = False
      degraded_reason: str | None = None
      requires_approval: bool = False
      pending_approval_step_id: str | None = None
      approval_granted: bool | None = None
      approved_step_ids: list[str] = Field(default_factory=list)
      approved_step_fingerprints: dict[str, str] = Field(default_factory=dict)
      pending_tool_approval: dict[str, Any] | None = None
      approved_tool_call_digests: dict[str, list[str]] = Field(default_factory=dict)
      final_answer: str | None = None
      last_turn_human_message_id: str | None = None
      thread_id: str | None = None
      metadata: dict[str, Any] = Field(default_factory=dict)
  ```
- **Luồng Thực Thi của Graph (`graph.py`)**:
  1. `build_turn_input` & `planner`: Mỗi lượt mới tạo `turn_id` duy nhất và reset toàn bộ trường phạm vi lượt (`plan`, `reflection_notes`, `reflection_count`, `degraded`, `degraded_reason`, `approved_tool_call_digests`, `pending_tool_approval`, `last_turn_human_message_id`), bảo đảm Reflector không bao giờ bị bỏ qua từ lượt thứ 3. `_apply_risk_policy` chỉ nâng cấp rủi ro theo từ khóa trên agent có tool đột biến (`coding` hoặc tool gated) nhằm tránh approval fatigue cho câu hỏi `chat`/`rag`.
  2. `hitl`: Kiểm tra rủi ro theo tool / `requires_approval` / `pending_tool_approval`. Nếu cần duyệt (kế hoạch hoặc yêu cầu nâng quyền `approval_required` ở pha thực thi tool), gọi `interrupt()` mang `tool_name` + `tool_arguments` cụ thể, cập nhật gauge `HITL_PENDING_APPROVALS` chống tăng đúp qua `_PENDING_APPROVAL_KEYS` khi replay node, lưu checkpoint và chờ phê duyệt qua `/api/v1/approvals` (xác minh `tenant_id` khớp và four-eyes ở `staging`/`production`).
  3. `dispatch`: Phân tích các step chạy song song/tuần tự qua A2A (`call_agent_stream`) hoặc Temporal (`AgentExecutionWorkflow` khi bật `use_temporal_dispatch` với workflow ID gắn `reflection_count` + nonce), truyền `tenant_id` từ JWT và `idempotency_key` gắn `tenant_id`/`owner_id`/`thread_id`/`turn_id`/`fp`, cấp step capability Ed25519 kèm `approved_args_digests`. Nếu sub-agent trả về `approval_required` (hoặc Temporal raise `ApplicationError` loại `ToolCallApprovalRequired`), gom vào danh sách chờ duyệt tất định theo `step_id` và quay lại `hitl` để duyệt ở mức tool-call.
  4. `fast_finalize` / `reflector`:
     - *Conversational Fast-Path*: Nếu bật `enable_chat_fast_path=True` và kế hoạch chỉ gồm 1 bước `chat` `low` risk hoàn tất sạch (`reflection_count == 0`, không `degraded`), đi thẳng tới `fast_finalize_node` (bỏ qua Reflector & synthesis Finalize).
     - *Reflector*: Với các luồng còn lại, kiểm tra đầu ra từ Sub-Agent (fail-closed khi judge lỗi) $\rightarrow$ *Pass* sang `finalize`, *Fail* tăng `reflection_count`, cập nhật `reflection_notes` và retry/re-plan.
  5. `finalize`: Tổng hợp câu trả lời cuối cùng với citations có cấu trúc, stream về L1 API qua SSE.

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
  - **Ingestion (`ingestion.py`) & Multi-Tenancy**: Hierarchical Node Parser (parent chunk 1024 tokens, child chunk 256 tokens). Tài liệu đặt dưới `data/raw/tenants/<tenant_id>/...` được tự động gắn `tenant_id` tương ứng vào metadata, UUIDv5 node ID và deduplication hash; tài liệu chung ở root giữ `"public"` và hỗ trợ truy cập tùy chọn qua cấu hình `rag_include_public_corpus` (`False` mặc định).
  - **Retriever (`retriever.py`)**: Hybrid Search = BM25 (lexical) + Dense Embedding (Qdrant/FAISS) thông qua Reciprocal Rank Fusion (RRF). Kỹ thuật Auto-merging retrieval để lấy context đầy đủ từ parent chunk khi nhiều child chunk khớp.
  - **Reranker (`reranker.py`)**: Sử dụng Cross-Encoder (`BAAI/bge-reranker-base`) để chấm lại điểm phù hợp trước khi đưa vào context window.
  - **Tool binding**: Cung cấp hàm `search_policy(query: str, filters: dict)` cho LangGraph ReAct loop của RAGAgent. Tenant (`tenant_id` từ JWT, không rơi về `owner_id`) và `user_roles`/`department` được ghim từ execution context đã xác thực (bỏ qua tham số từ LLM); RBAC fail-closed được cưỡng chế trên cả `staging` và `production`.
  - **Bootstrap & Incremental CDC**: Job `agentic-rag-bootstrap` (cấu hình `HF_HOME="/opt/hf-cache"`, `HF_HUB_OFFLINE="1"`) lập chỉ mục ban đầu; thay đổi đơn lẻ đi qua `ingest_single_document` / `delete_source` (tự động tỉa leaf node cũ khỏi `docstore.json` để tránh BM25 trả kết quả stale) và endpoint nội bộ `POST /api/v1/admin/rag/cdc`.

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
- **Ràng buộc an toàn**: Các thao tác ghi file (`repo_write_file`), thực thi Python (`code_exec_python`), hoặc chạy lệnh bash (`code_exec_bash`) đều kích hoạt phê duyệt HITL 2 pha theo tool-call (`build_react_graph` gắn `_handle_react_tool_error` trên `ToolNode` bảo đảm `ToolCallApprovalRequired` và `PermissionError` không bao giờ bị nuốt).

---

## 5. Tài Liệu Kiểm Định & Tham Chiếu Liên Quan

- **Kiểm định kiến trúc và giao thức A2A**: [REVIEW_ARCHITECTURE & PROTOCOLS.md](../reviews/REVIEW_ARCHITECTURE%20&%20PROTOCOLS.md)
- **Kiểm định chính sách bảo mật và HITL**: [REVIEW_SECURITY, GUARDRAILS & HITL.md](../reviews/REVIEW_SECURITY,%20GUARDRAILS%20&%20HITL.md)
- **Kiểm định chất lượng mô hình & RAG**: [REVIEW_QUALITY GATES - DEEPEVAL & RAGAS.md](../reviews/REVIEW_QUALITY%20GATES%20-%20DEEPEVAL%20&%20RAGAS.md)
