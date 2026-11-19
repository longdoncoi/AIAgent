# AGENTS.md — Cẩm Nang & Đặc Tả Hệ Thống Multi-Agent (Agentic Platform)

> **Tài liệu tham chiếu chuẩn** dành cho các kỹ sư phát triển và các AI Coding Agent làm việc trong kho mã nguồn `agentic-platform`.  
> Xây dựng dựa trên kiến trúc tham chiếu tại [Architecture.md](file:///f:/PROJECTS/AIAgent/Architecture.md).  
> 💡 **Chỉ dẫn tác chiến cho AI Coding Agent**: Xem chi tiết tại [agent-instructions.md](file:///f:/PROJECTS/AIAgent/.context/agent-instructions.md).

---

## 1. Tổng Quan Hệ Thống & Triết Lý Thiết Kế

Hệ thống được thiết kế theo mô hình **Hierarchical Multi-Agent (Đa tác tử phân cấp)**:

- **L2 Supervisor (LangGraph)** đóng vai trò tổng chỉ huy (Orchestrator): Lập kế hoạch (Plan), điều phối song song (Dispatch/Fan-out), vòng lặp suy luận - hành động (ReAct), tự phản tỉnh (Reflection) và tạm dừng chờ duyệt (HITL).
- **L3 Sub-Agents** hoạt động như các microservices độc lập giao tiếp qua chuẩn **A2A (Agent-to-Agent)** protocol.
- **L4 Tools** kết nối chuẩn hóa qua **Model Context Protocol (MCP)**, chạy trong sandbox cách ly.
- **L5 Knowledge (RAG)** vận hành qua **LlamaIndex** với cơ chế chunking phân cấp, hybrid search và cross-encoder reranking.
- **L7 Model Gateway** trừu tượng hóa mô hình nền tảng qua **LiteLLM**, trỏ tới cụm GPU (vLLM) hoặc CPU/edge (llama.cpp).

### Các Nguyên Tắc Bất Biến (Design Inviolables)

1. **Dependency Rule**: Tầng trên gọi tầng dưới theo đúng thứ tự phân lớp. Tầng dưới tuyệt đối không import hay phụ thuộc ngược tầng trên (`core/` là kernel độc lập).
2. **Stateless Services**: Mọi Agent và Sub-agent đều stateless. State duy nhất của phiên làm việc do LangGraph Supervisor quản lý và lưu trữ ngoại vi (Postgres Checkpointer, Qdrant/FAISS).
3. **Chuẩn hóa Giao thức**:
   - Agent ↔ Agent: **A2A protocol** (HTTP/gRPC với `AgentCard` định nghĩa capabilities).
   - Agent ↔ Tool: **MCP (Model Context Protocol)**.
   - Agent ↔ Model: **OpenAI-compatible API** qua LiteLLM Gateway.
4. **Resumable & Replayable**: Tất cả các bước đều được checkpoint. HITL, retry và replay lỗi đều dùng chung cơ chế snapshot của LangGraph.
5. **Cross-cutting Safety & Observability**: Guardrails, PII check, Tracing (OpenTelemetry/LangSmith) và Metrics được tích hợp đồng bộ ở mọi tầng ranh giới.

---

## 2. Bảng Tổng Hợp Danh Mục Tác Tử (Agent Registry Matrix)

| Tác Tử (Agent Name) | Phân Tầng | Giao Thức / Port | LLM Alias | Vai Trò & Trách Nhiệm Chính | Bộ Công Cụ (MCP / RAG Tools) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Supervisor** | L2 Orchestration | Internal LangGraph | `reasoning-llm` (Qwen3-72B / DeepSeek-R1) | Phân tích bài toán, lập DAG kế hoạch, dispatch song song, tổng hợp kết quả, HITL | A2A Client to Sub-Agents |
| **ChatAgent** | L3 Sub-Agent | A2A / `http://localhost:9100` | `fast-llm` (Qwen3-14B) | Đàm thoại ngữ cảnh thông thường, tóm tắt, giải thích, làm rõ yêu cầu người dùng | Web Search Tool (tùy chọn) |
| **RAGAgent** | L3 Sub-Agent | A2A / `http://localhost:9101` | `reasoning-llm` | Truy xuất tri thức nội bộ, chính sách, tài liệu kỹ thuật, bảo đảm căn cứ (groundedness) | `search_policy`, LlamaIndex Hybrid Retriever |
| **CodingAgent** | L3 Sub-Agent | A2A / `http://localhost:9102` | `coding-llm` (Qwen3-Coder / Claude) | Đọc hiểu mã nguồn, sinh code, chạy thử nghiệm trong sandbox, sửa lỗi và refactor | `code_exec_server`, `repo_server` |
| **Planner (Node)** | L2 Internal Node | LangGraph Node | `reasoning-llm` | Phân rã câu hỏi phức tạp thành danh sách các bước (`Plan` gồm các `Step`) | None (Pure Reasoning) |
| **Reflector (Node)** | L2 Internal Node | LangGraph Node | `reasoning-llm` | Đánh giá tính chính xác, phát hiện hallucination, quyết định hoàn thành hay retry/re-plan | None (Self-Correction Critique) |

---

## 3. Bản Đồ Phân Vùng Kiến Trúc Chuyên Sâu (Agent Subsystem Domains)

Để đảm bảo tính module hóa cao độ và triệt tiêu trùng lặp dữ liệu, toàn bộ các đặc tả kỹ thuật chi tiết của hệ thống được phân rã thành 6 domain độc lập đặt tại thư mục [.workflows/specifications/](file:///f:/PROJECTS/AIAgent/.workflows/specifications/):

| STT | Domain Kiến Trúc | Tài Liệu Chi Tiết | Tóm Tắt Phạm Vi & Nhiệm Vụ |
| :---: | :--- | :--- | :--- |
| **01** | **Đặc Tả Chi Tiết Từng Agent** | [agent-specifications.md](file:///f:/PROJECTS/AIAgent/.workflows/specifications/agent-specifications.md) | Đặc tả cấu trúc LangGraph, State Schema (Step, Plan, AgentState), luồng thực thi, A2A endpoints và capabilities của Supervisor, ChatAgent, RAGAgent, CodingAgent. |
| **02** | **Giao Thức Tương Tác & Kết Nối** | [protocols-networking.md](file:///f:/PROJECTS/AIAgent/.workflows/specifications/protocols-networking.md) | Đặc tả giao thức A2A (JSON schema), công cụ MCP (FastMCP & Allowlist), Bản đồ phân phối cổng mạng, Caching đa tầng (RAG, Prompt, KV-Cache) và tối ưu kết nối tải cao. |
| **03** | **Quản Lý Bộ Nhớ & Trạng Thái** | [state-memory.md](file:///f:/PROJECTS/AIAgent/.workflows/specifications/state-memory.md) | Kiến trúc bộ nhớ L6: Short-term checkpointer (Postgres/Redis HMAC), Long-term memory (Semantic, Episodic, Procedural), quản lý lược đồ Alembic và Consolidation Node. |
| **04** | **Chính Sách An Toàn & Phê Duyệt** | [safety-hitl.md](file:///f:/PROJECTS/AIAgent/.workflows/specifications/safety-hitl.md) | Cơ chế phòng thủ chiều sâu L8: Input/Output Guardrails, phân loại 3 mức độ rủi ro (Low/Medium/High) và quy trình tạm dừng duyệt can thiệp của con người (HITL). |
| **05** | **Quan Sát & Đánh Giá Chất Lượng** | [observability-evals.md](file:///f:/PROJECTS/AIAgent/.workflows/specifications/observability-evals.md) | Giám sát toàn trình L9: Truy vết phân tán qua OpenTelemetry/LangSmith, chỉ số Prometheus định lượng và khung đánh giá chất lượng tự động RAGAS & DeepEval. |
| **06** | **Hướng Dẫn Phát Triển Cho AI & Dev** | [developer-guidelines.md](file:///f:/PROJECTS/AIAgent/.workflows/specifications/developer-guidelines.md) | Bộ quy chuẩn lập trình Python 3.12, quy trình thêm mới Sub-Agent, thêm mới MCP Tool và danh mục các lệnh vận hành dự án qua Makefile. |

---

## 4. Hệ Thống Báo Cáo Kiểm Định Vận Hành (Production Readiness Reviews)

Hệ thống tài liệu kiểm định độ sẵn sàng vận hành Production (PRR) gồm 14 trụ cột kỹ thuật chuyên biệt và 4 quy trình quản trị vận hành được quản lý tập trung tại thư mục [.workflows/reviews/](file:///f:/PROJECTS/AIAgent/.workflows/reviews/), kết nối trực tiếp với cẩm nang tổng thể tại [REVIEW.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW.md):

- **14 Trụ Cột Kỹ Thuật**:
  - **Kiến Trúc & Giao Thức**: [REVIEW_ARCHITECTURE & PROTOCOLS.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_ARCHITECTURE%20&%20PROTOCOLS.md)
  - **Bộ Nhớ & Dữ Liệu**: [REVIEW_STATE, MEMORY & PERSISTENCE.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_STATE,%20MEMORY%20&%20PERSISTENCE.md)
  - **Bộ Đệm Đa Tầng**: [REVIEW_MULTI-TIER CACHING SUBSYSTEM.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_MULTI-TIER%20CACHING%20SUBSYSTEM.md)
  - **Bảo Mật & HITL**: [REVIEW_SECURITY, GUARDRAILS & HITL.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_SECURITY,%20GUARDRAILS%20&%20HITL.md)
  - **Cổng Mô Hình (LLM)**: [REVIEW_MODEL GATEWAY & INFERENCE.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_MODEL%20GATEWAY%20&%20INFERENCE.md)
  - **Chất Lượng Tác Tử (AI)**: [REVIEW_QUALITY GATES - DEEPEVAL & RAGAS.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_QUALITY%20GATES%20-%20DEEPEVAL%20&%20RAGAS.md)
  - **Hạ Tầng Kubernetes & SRE**: [REVIEW_KUBERNETES & SRE.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_KUBERNETES%20&%20SRE.md)
  - **Giám Sát & Cảnh Báo**: [REVIEW_OBSERVABILITY & ALERTING.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_OBSERVABILITY%20&%20ALERTING.md)
  - **Quy Trình CI/CD & GitOps**: [REVIEW_CI-CD & GITOPS.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_CI-CD%20&%20GITOPS.md)
  - **Vận Hành & Khôi Phục Thảm Họa**: [REVIEW_SRE & DISASTER RECOVERY.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_SRE%20&%20DISASTER%20RECOVERY.md)
  - **Chiến Lược Kiểm Thử Toàn Diện**: [REVIEW_TESTING & QA STRATEGY.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_TESTING%20&%20QA%20STRATEGY.md)
  - **Tuân Thủ Pháp Lý & Đạo Đức AI**: [REVIEW_COMPLIANCE, PRIVACY & AI ETHICS.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_COMPLIANCE,%20PRIVACY%20&%20AI%20ETHICS.md)
  - **Quản Trị Chi Phí (FinOps)**: [REVIEW_FINOPS & COST GOVERNANCE.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_FINOPS%20&%20COST%20GOVERNANCE.md)
  - **Quản Trị Dữ Liệu & Phân Quyền RAG**: [REVIEW_DATA GOVERNANCE & RAG RBAC.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_DATA%20GOVERNANCE%20&%20RAG%20RBAC.md)
- **4 Khung Quản Trị & Vận Hành**:
  - **Quy Chuẩn Kiểm Định & Phân Quyền RACI**: [REVIEW_GOVERNANCE & AUDIT STANDARDS.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_GOVERNANCE%20&%20AUDIT%20STANDARDS.md)
  - **Quy Trình 5 Cổng Diễn Tập Trước Giờ G**: [REVIEW_PRE-FLIGHT STAGE GATES.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_PRE-FLIGHT%20STAGE%20GATES.md)
  - **Quản Trị Giám Sát Sau Triển Khai (Hypercare)**: [REVIEW_HYPERCARE & INCIDENT ESCALATION.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_HYPERCARE%20&%20INCIDENT%20ESCALATION.md)
  - **Biên Bản Nghiệm Thu & Phê Chuẩn Go-Live**: [REVIEW_SIGN-OFF & GO-LIVE APPROVAL.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_SIGN-OFF%20&%20GO-LIVE%20APPROVAL.md)

---

*Tài liệu này được bảo trì đồng bộ cùng kiến trúc hệ thống. Mọi thay đổi về phân tầng hoặc giao thức mạng bắt buộc phải cập nhật lại tài liệu này và các tài liệu thành phần trong thư mục [.workflows/](file:///f:/PROJECTS/AIAgent/.workflows/) trước khi tạo Pull Request.*
