# AGENTS.md — Cẩm Nang & Đặc Tả Hệ Thống Multi-Agent (Agentic Platform)

> **Tài liệu tham chiếu chuẩn** dành cho các kỹ sư phát triển và các AI Coding Agent làm việc trong kho mã nguồn `agentic-platform`.  
> Xây dựng dựa trên kiến trúc tham chiếu tại [Architecture.md](Architecture.md).  
> 💡 **Chỉ dẫn tác chiến cho AI Coding Agent**: Xem chi tiết tại [agent-instructions.md](.context/agent-instructions.md).

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
   - Agent ↔ Agent: **A2A-inspired REST/SSE profile nội bộ** (`/a2a/tasks`, chưa phải JSON-RPC A2A SDK) với `AgentCard` định nghĩa capabilities và envelope Ed25519.
   - Agent ↔ Tool: **MCP (Model Context Protocol)**.
   - Agent ↔ Model: **OpenAI-compatible API** qua LiteLLM Gateway.
4. **Resumable & Replayable**: Tất cả các bước đều được checkpoint. HITL, retry và replay lỗi đều dùng chung cơ chế snapshot của LangGraph.
5. **Cross-cutting Safety & Observability**: Guardrails, PII check, Tracing (OpenTelemetry/LangSmith) và Metrics được tích hợp đồng bộ ở mọi tầng ranh giới.

---

## 2. Bảng Tổng Hợp Danh Mục Tác Tử (Agent Registry Matrix)

| Tác Tử (Agent Name) | Phân Tầng | Giao Thức / Port | LLM Alias | Vai Trò & Trách Nhiệm Chính | Bộ Công Cụ (MCP / RAG Tools) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Supervisor** | L2 Orchestration | Internal LangGraph | `reasoning-llm` (Qwen2.5-72B-Instruct) | Phân tích bài toán, lập DAG kế hoạch, dispatch song song, tổng hợp kết quả, HITL | A2A Client to Sub-Agents |
| **ChatAgent** | L3 Sub-Agent | A2A-inspired REST/SSE / `http://localhost:9100` | `fast-llm` (Qwen2.5-14B-Instruct) | Đàm thoại ngữ cảnh thông thường, tóm tắt, giải thích, làm rõ yêu cầu người dùng | Web Search Tool (tùy chọn) |
| **RAGAgent** | L3 Sub-Agent | A2A / `http://localhost:9101` | `reasoning-llm` | Truy xuất tri thức nội bộ, chính sách, tài liệu kỹ thuật, bảo đảm căn cứ (groundedness) | `search_policy`, LlamaIndex Hybrid Retriever |
| **CodingAgent** | L3 Sub-Agent | A2A-inspired REST/SSE / `http://localhost:9102` | `coding-llm` (Qwen2.5-Coder-32B-Instruct) | Đọc hiểu mã nguồn, sinh code, chạy thử nghiệm trong sandbox, sửa lỗi và refactor | `code_exec_server`, `repo_server` |
| **Planner (Node)** | L2 Internal Node | LangGraph Node | `reasoning-llm` | Phân rã câu hỏi phức tạp thành danh sách các bước (`Plan` gồm các `Step`) | None (Pure Reasoning) |
| **Reflector (Node)** | L2 Internal Node | LangGraph Node | `reasoning-llm` | Đánh giá tính chính xác, phát hiện hallucination, quyết định hoàn thành hay retry/re-plan | None (Self-Correction Critique) |

---

## 3. Bản Đồ Phân Vùng Kiến Trúc Chuyên Sâu (Agent Subsystem Domains)

Để đảm bảo tính module hóa cao độ và triệt tiêu trùng lặp dữ liệu, toàn bộ các đặc tả kỹ thuật chi tiết của hệ thống được phân rã thành 6 domain độc lập đặt tại thư mục [.workflows/specifications/](.workflows/specifications/):

| STT | Domain Kiến Trúc | Tài Liệu Chi Tiết | Tóm Tắt Phạm Vi & Nhiệm Vụ |
| :---: | :--- | :--- | :--- |
| **01** | **Đặc Tả Chi Tiết Từng Agent** | [agent-specifications.md](.workflows/specifications/agent-specifications.md) | Đặc tả LangGraph, `AgentState` (`last_turn_human_message_id` + `build_turn_input` reset đa lượt), conversational fast-path (`enable_chat_fast_path` / `fast_finalize_node`), multi-tenant RAG (`data/raw/tenants/<id>/` + `rag_include_public_corpus`), A2A endpoints và hai pha rủi ro cho `CodingAgent`. |
| **02** | **Giao Thức Tương Tác & Kết Nối** | [protocols-networking.md](.workflows/specifications/protocols-networking.md) | Giao thức A2A (JSON schema + `idempotency_key` khóa theo `tenant_id`/`owner_id`/`thread_id`/`turn_id`/`fp`), MCP (`MCP_TRANSPORT=streamable-http` trên `/mcp`, `httpx2` cho `mcp>=2.0.0`, Default-Deny Allowlist), Ports + manifest K8s `serving.yaml`/`datastores.yaml`, Caching đa tầng (lazy neural embedder, quantifier guard, shared tenant scope, `[CITATIONS]`-safe redaction) và connection pooling. |
| **03** | **Quản Lý Bộ Nhớ & Trạng Thái** | [state-memory.md](.workflows/specifications/state-memory.md) | Kiến trúc bộ nhớ L6 (`AsyncPostgresSaver`, tách `redis-state`/`redis-cache`, RTBF fan-out kèm `actor_id`/`target_user_id`), xoay vòng `checkpoint_hmac_previous_secrets`, kiểm chứng chuỗi hash qua `verify_audit_chain.py`, diễn tập DR `restore_drill.py`, Alembic `004_audit_seq_checkpoints` (`AUTO_RUN_MIGRATIONS=false`), ArgoCD Sync Waves và PVC `efs-sc`. |
| **04** | **Chính Sách An Toàn & Phê Duyệt** | [safety-hitl.md](.workflows/specifications/safety-hitl.md) | Phòng thủ L8 áp dụng đồng nhất cho cả `staging` và `production` (`is_strict_environment`, `require_authentication=true` fail-closed, bỏ dev token khỏi `development`), OIDC/JWKS + fail-closed JTI revocation, phê duyệt 2 pha qua `ToolNode` cho `code_exec_bash`, `code_exec_python` và `repo_write_file`, xoay vòng khóa `kid`, Guardrails (PII VN + toxicity + ZWSP base64) và Istio mTLS `STRICT`. |
| **05** | **Quan Sát & Đánh Giá Chất Lượng** | [observability-evals.md](.workflows/specifications/observability-evals.md) | Giám sát L9 (OpenTelemetry/LangSmith redacted PII/secrets, `HITL_PENDING_APPROVALS` chống rò khi replay, `agent_model_proxy_fallback_total` trong `ModelCircuitBreakerTripped`, kube-state-metrics cho `GPUOOMAlert`), mục tiêu SLO/TTFT (`docs/slos.md`), bộ eval mở rộng (`100` routing / `50` RAG / `60` red-team), chấm từ chối rỗng, `planner_mode` + `release_gate.py`. |
| **06** | **Hướng Dẫn Phát Triển Cho AI & Dev** | [developer-guidelines.md](.workflows/specifications/developer-guidelines.md) | Bộ quy chuẩn Python 3.12 (`pyproject.toml` + `uv.lock`), thêm Sub-Agent / MCP Tool, production Kustomize, Gitleaks full-history, CycloneDX SBOM + BuildKit provenance + Cosign keyless, bảo vệ `seed_eval.py --force`, và script vận hành (`verify_audit_chain.py`, `restore_drill.py`). |

---

## 4. Hệ Thống Báo Cáo Kiểm Định Vận Hành (Production Readiness Reviews)

Hệ thống tài liệu kiểm định độ sẵn sàng vận hành Production (PRR) gồm 14 trụ cột kỹ thuật chuyên biệt và 4 quy trình quản trị vận hành được quản lý tập trung tại thư mục [.workflows/reviews/](.workflows/reviews/), kết nối trực tiếp với cẩm nang tổng thể tại [REVIEW.md](.workflows/reviews/REVIEW.md):

- **14 Trụ Cột Kỹ Thuật**:
  - **Kiến Trúc & Giao Thức**: [REVIEW_ARCHITECTURE & PROTOCOLS.md](.workflows/reviews/REVIEW_ARCHITECTURE%20&%20PROTOCOLS.md)
  - **Bộ Nhớ & Dữ Liệu**: [REVIEW_STATE, MEMORY & PERSISTENCE.md](.workflows/reviews/REVIEW_STATE,%20MEMORY%20&%20PERSISTENCE.md)
  - **Bộ Đệm Đa Tầng**: [REVIEW_MULTI-TIER CACHING SUBSYSTEM.md](.workflows/reviews/REVIEW_MULTI-TIER%20CACHING%20SUBSYSTEM.md)
  - **Bảo Mật & HITL**: [REVIEW_SECURITY, GUARDRAILS & HITL.md](.workflows/reviews/REVIEW_SECURITY,%20GUARDRAILS%20&%20HITL.md)
  - **Cổng Mô Hình (LLM)**: [REVIEW_MODEL GATEWAY & INFERENCE.md](.workflows/reviews/REVIEW_MODEL%20GATEWAY%20&%20INFERENCE.md)
  - **Chất Lượng Tác Tử (AI)**: [REVIEW_QUALITY GATES - DEEPEVAL & RAGAS.md](.workflows/reviews/REVIEW_QUALITY%20GATES%20-%20DEEPEVAL%20&%20RAGAS.md)
  - **Hạ Tầng Kubernetes & SRE**: [REVIEW_KUBERNETES & SRE.md](.workflows/reviews/REVIEW_KUBERNETES%20&%20SRE.md)
  - **Giám Sát & Cảnh Báo**: [REVIEW_OBSERVABILITY & ALERTING.md](.workflows/reviews/REVIEW_OBSERVABILITY%20&%20ALERTING.md)
  - **Quy Trình CI/CD & GitOps**: [REVIEW_CI-CD & GITOPS.md](.workflows/reviews/REVIEW_CI-CD%20&%20GITOPS.md)
  - **Vận Hành & Khôi Phục Thảm Họa**: [REVIEW_SRE & DISASTER RECOVERY.md](.workflows/reviews/REVIEW_SRE%20&%20DISASTER%20RECOVERY.md)
  - **Chiến Lược Kiểm Thử Toàn Diện**: [REVIEW_TESTING & QA STRATEGY.md](.workflows/reviews/REVIEW_TESTING%20&%20QA%20STRATEGY.md)
  - **Tuân Thủ Pháp Lý & Đạo Đức AI**: [REVIEW_COMPLIANCE, PRIVACY & AI ETHICS.md](.workflows/reviews/REVIEW_COMPLIANCE,%20PRIVACY%20&%20AI%20ETHICS.md)
  - **Quản Trị Chi Phí (FinOps)**: [REVIEW_FINOPS & COST GOVERNANCE.md](.workflows/reviews/REVIEW_FINOPS%20&%20COST%20GOVERNANCE.md)
  - **Quản Trị Dữ Liệu & Phân Quyền RAG**: [REVIEW_DATA GOVERNANCE & RAG RBAC.md](.workflows/reviews/REVIEW_DATA%20GOVERNANCE%20&%20RAG%20RBAC.md)
- **4 Khung Quản Trị & Vận Hành**:
  - **Quy Chuẩn Kiểm Định & Phân Quyền RACI**: [REVIEW_GOVERNANCE & AUDIT STANDARDS.md](.workflows/reviews/REVIEW_GOVERNANCE%20&%20AUDIT%20STANDARDS.md)
  - **Quy Trình 5 Cổng Diễn Tập Trước Giờ G**: [REVIEW_PRE-FLIGHT STAGE GATES.md](.workflows/reviews/REVIEW_PRE-FLIGHT%20STAGE%20GATES.md)
  - **Quản Trị Giám Sát Sau Triển Khai (Hypercare)**: [REVIEW_HYPERCARE & INCIDENT ESCALATION.md](.workflows/reviews/REVIEW_HYPERCARE%20&%20INCIDENT%20ESCALATION.md)
  - **Biên Bản Nghiệm Thu & Phê Chuẩn Go-Live**: [REVIEW_SIGN-OFF & GO-LIVE APPROVAL.md](.workflows/reviews/REVIEW_SIGN-OFF%20&%20GO-LIVE%20APPROVAL.md)

---

*Tài liệu này được bảo trì đồng bộ cùng kiến trúc hệ thống. Mọi thay đổi về phân tầng hoặc giao thức mạng bắt buộc phải cập nhật lại tài liệu này và các tài liệu thành phần trong thư mục [.workflows/](.workflows/) trước khi tạo Pull Request.*
