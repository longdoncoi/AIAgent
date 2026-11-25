# .workflows — Quy Trình Thao Tác Chuẩn (Standard Operating Procedures)

> Thư mục này định nghĩa các quy trình vận hành chuẩn (SOP), các tài liệu đặc tả kỹ thuật và tiêu chuẩn kiểm định độ sẵn sàng phát hành (PRR) dành cho toàn bộ kỹ sư và AI Coding Agent trong dự án `agentic-platform`.

---

## 1. Bản Đồ Quy Trình (Workflow Directory Map)

| Danh mục | Tài liệu | Mô tả |
| :--- | :--- | :--- |
| **Quy trình Lập trình** | [coding-workflow.md](coding-workflow.md) | Quy chuẩn viết code Python 3.12, quy tắc thêm Sub-Agent, MCP Tool và các lệnh vận hành. |
| **Vòng đời Tính năng** | [feature-lifecycle.md](feature-lifecycle.md) | Vòng đời 5 bước phát triển tính năng mới: Khảo sát -> Spec -> TDD -> Implement -> Verify. |
| **Quy trình Sửa lỗi** | [bugfix-workflow.md](bugfix-workflow.md) | Quy trình tái hiện lỗi, định vị nguyên nhân gốc, vá lỗi tối thiểu và cập nhật memory. |
| **Kiểm tra Pre-flight** | [pr-preflight.md](pr-preflight.md) | Checklist kiểm soát chất lượng nghiêm ngặt trước khi tạo Pull Request. |

---

## 2. Thư Mục Đặc Tả Kỹ Thuật (Specifications)

Toàn bộ đặc tả kỹ thuật chi tiết của hệ thống Multi-Agent nằm tại [specifications/](specifications):
- [agent-specifications.md](specifications/agent-specifications.md): Đặc tả LangGraph Supervisor, ChatAgent, RAGAgent, CodingAgent.
- [protocols-networking.md](specifications/protocols-networking.md): Giao thức A2A, FastMCP Tools, Port Allocation, Caching đa tầng.
- [state-memory.md](specifications/state-memory.md): Phân tầng bộ nhớ L6, Checkpointer (Postgres/Redis HMAC), Alembic.
- [safety-hitl.md](specifications/safety-hitl.md): Phòng vệ đa tầng L8, Guardrails, phân cấp rủi ro và Human-in-the-Loop.
- [observability-evals.md](specifications/observability-evals.md): OpenTelemetry, LangSmith, Prometheus Metrics, DeepEval, RAGAS.
- [developer-guidelines.md](specifications/developer-guidelines.md): Hướng dẫn chi tiết cho AI & Kỹ sư phát triển.

---

## 3. Khung Đánh Giá Sẵn Sàng Vận Hành (Production Readiness Reviews)

Hội đồng kiểm định PRR và toàn bộ 14 trụ cột kiểm định kỹ thuật & 4 khung quản trị nằm tại [reviews/](reviews):
- **Cẩm Nang & Dashboard Tổng Thể**: [REVIEW.md](reviews/REVIEW.md) (Master Orchestrator, P0/P1 Criteria & Readiness Dashboard).
- **14 Trụ cột Kỹ thuật**: Architecture & Protocols, State & Persistence, Caching, Security & HITL, Model Gateway, Quality Gates (DeepEval & RAGAS), Kubernetes & SRE, Observability, CI/CD & GitOps, Disaster Recovery, Testing & QA, Compliance & Privacy, FinOps, Data Governance.
- **4 Khung Quản trị**: Governance & RACI, Pre-flight Stage Gates, Hypercare & Escalation, Sign-off & Go-Live Approval.
