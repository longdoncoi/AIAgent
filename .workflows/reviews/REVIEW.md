# REVIEW.md — Cẩm Nang & Checklist Đánh Giá Độ Sẵn Sàng Production (PRR)

> **Tài liệu kiểm định kỹ thuật chính thức cấp quản lý** cho Hội đồng Đánh giá Phát hành (Release Board).  
> **Dự án**: `agentic-platform` (Multi-Agent Doanh Nghiệp Phân Cấp: LangGraph · vLLM · llama.cpp · LlamaIndex · MCP · A2A · Temporal · Kubernetes)  
> **Phiên bản tài liệu**: `v1.0.0-PROD-AUDIT` · **Tài liệu tham chiếu cơ sở**: [ARCHITECTURE.md](../../Architecture.md) · [AGENTS.md](../../AGENTS.md)

---

## 1. Tổng Quan Khung Đánh Giá PRR (Executive Overview)

Tài liệu này đóng vai trò **Bộ chỉ huy trung tâm (Master Orchestrator)** điều phối toàn bộ tiêu chuẩn đo lường và quy trình kiểm định trước khi kích hoạt phát hành Production.  
Toàn bộ hệ thống kiểm định gồm **14 Trụ cột Kỹ thuật** và **4 Khung Quản trị Vận hành** được quản lý độc lập tại thư mục [.workflows/reviews/](../../.workflows/reviews/):
- **Điều kiện Go-Live tối thiểu**: 100% tiêu chí P0 đạt (`44/44`), $\ge 90\%$ tiêu chí P1 đạt (`25/27`), Điểm tổng thể $\ge 95.0\%$. Chỉ cần **1 lỗi P0** chưa giải quyết $\rightarrow$ **Lập tức No-Go** (không ngoại lệ).

---

## 2. Bảng Tóm Tắt Trạng Thái Sẵn Sàng (Executive Readiness Dashboard)

| STT | Trụ Cột Kỹ Thuật Đánh Giá | Tệp Đặc Tả Chuyên Biệt | Đơn Vị Phụ Trách | Số Tiêu Chí (P0/P1/P2) | Trọng Số | Trạng Thái P0 |
| :---: | :--- | :--- | :--- | :---: | :---: | :---: |
| **01** | **Kiến Trúc & Giao Thức** | [REVIEW_ARCHITECTURE & PROTOCOLS.md](../../.workflows/reviews/REVIEW_ARCHITECTURE%20&%20PROTOCOLS.md) | Lead Architect & Backend Lead | 6 (4 P0 / 2 P1) | 8% | [ ] 4/4 P0 |
| **02** | **Bộ Nhớ & Dữ Liệu** | [REVIEW_STATE, MEMORY & PERSISTENCE.md](../../.workflows/reviews/REVIEW_STATE,%20MEMORY%20&%20PERSISTENCE.md) | Lead Backend & DBA | 6 (3 P0 / 3 P1) | 8% | [ ] 3/3 P0 |
| **03** | **Bộ Đệm Đa Tầng** | [REVIEW_MULTI-TIER CACHING SUBSYSTEM.md](../../.workflows/reviews/REVIEW_MULTI-TIER%20CACHING%20SUBSYSTEM.md) | Performance & Backend Lead | 4 (2 P0 / 2 P1) | 7% | [ ] 2/2 P0 |
| **04** | **Bảo Mật & HITL** | [REVIEW_SECURITY, GUARDRAILS & HITL.md](../../.workflows/reviews/REVIEW_SECURITY,%20GUARDRAILS%20&%20HITL.md) | SecOps / Cyber Security Lead | 6 (5 P0 / 1 P1) | 10% | [ ] 5/5 P0 |
| **05** | **Cổng Mô Hình (LLM)** | [REVIEW_MODEL GATEWAY & INFERENCE.md](../../.workflows/reviews/REVIEW_MODEL%20GATEWAY%20&%20INFERENCE.md) | AI Serving / Infra Lead | 4 (2 P0 / 2 P1) | 8% | [ ] 2/2 P0 |
| **06** | **Chất Lượng Tác Tử (AI)**| [REVIEW_QUALITY GATES - DEEPEVAL & RAGAS.md](../../.workflows/reviews/REVIEW_QUALITY%20GATES%20-%20DEEPEVAL%20&%20RAGAS.md) | AI Evaluation & Data Quality | 5 (3 P0 / 2 P1) | 10% | [ ] 3/3 P0 |
| **07** | **Kubernetes & SRE** | [REVIEW_KUBERNETES & SRE.md](../../.workflows/reviews/REVIEW_KUBERNETES%20&%20SRE.md) | Principal SRE & Cloud Architect | 7 (5 P0 / 2 P1) | 8% | [ ] 5/5 P0 |
| **08** | **Giám Sát & Cảnh Báo** | [REVIEW_OBSERVABILITY & ALERTING.md](../../.workflows/reviews/REVIEW_OBSERVABILITY%20&%20ALERTING.md) | SRE Monitoring & Backend Lead | 4 (2 P0 / 2 P1) | 7% | [ ] 2/2 P0 |
| **09** | **CI/CD & GitOps** | [REVIEW_CI-CD & GITOPS.md](../../.workflows/reviews/REVIEW_CI-CD%20&%20GITOPS.md) | DevOps / Release Lead | 5 (4 P0 / 1 P1) | 5% | [ ] 4/4 P0 |
| **10** | **Vận Hành & Khôi Phục** | [REVIEW_SRE & DISASTER RECOVERY.md](../../.workflows/reviews/REVIEW_SRE%20&%20DISASTER%20RECOVERY.md) | Incident Commander & Lead SRE | 4 (2 P0 / 2 P1) | 5% | [ ] 2/2 P0 |
| **11** | **Chiến Lược Test & QA** | [REVIEW_TESTING & QA STRATEGY.md](../../.workflows/reviews/REVIEW_TESTING%20&%20QA%20STRATEGY.md) | QA Lead & Principal Tester | 6 (4 P0 / 2 P1) | 8% | [ ] 4/4 P0 |
| **12** | **Pháp Lý & Đạo Đức AI**| [REVIEW_COMPLIANCE, PRIVACY & AI ETHICS.md](../../.workflows/reviews/REVIEW_COMPLIANCE,%20PRIVACY%20&%20AI%20ETHICS.md) | Legal Counsel & DPO Lead | 6 (4 P0 / 2 P1) | 6% | [ ] 4/4 P0 |
| **13** | **Quản Trị Chi Phí (FinOps)**| [REVIEW_FINOPS & COST GOVERNANCE.md](../../.workflows/reviews/REVIEW_FINOPS%20&%20COST%20GOVERNANCE.md) | FinOps Lead & Cloud Architect | 5 (2 P0 / 2 P1 / 1 P2) | 5% | [ ] 2/2 P0 |
| **14** | **Quản Trị Tri Thức RAG**| [REVIEW_DATA GOVERNANCE & RAG RBAC.md](../../.workflows/reviews/REVIEW_DATA%20GOVERNANCE%20&%20RAG%20RBAC.md) | Data Governance & RAG Lead | 5 (2 P0 / 2 P1 / 1 P2) | 5% | [ ] 2/2 P0 |
| **TỔNG**| **14 TRỤ CỘT KỸ THUẬT**| **14 TỆP CHUYÊN BIỆT** | **HỘI ĐỒNG THẨM ĐỊNH (9 LEADS)** | **73 TIÊU CHÍ (44 P0 / 27 P1 / 2 P2)**| **100%** | **[ ] 44/44 P0** |

---

## 3. Khung Quy Trình Quản Trị, Diễn Tập & Nghiệm Thu (Governance & Operational Domains)

Bên cạnh 14 trụ cột kỹ thuật, quy trình kiểm định và vận hành phát hành được chuẩn hóa thành 4 domain nghiệp vụ chuyên biệt:

| STT | Domain Quản Trị & Vận Hành | Tài Liệu Chi Tiết | Đơn Vị Chủ Trì | Nội Dung Trọng Tâm & Nhiệm Vụ |
| :---: | :--- | :--- | :--- | :--- |
| **01** | **Quy Chuẩn Kiểm Định & Phân Quyền RACI** | [REVIEW_GOVERNANCE & AUDIT STANDARDS.md](../../.workflows/reviews/REVIEW_GOVERNANCE%20&%20AUDIT%20STANDARDS.md) | Chief AI Architect & SecOps Lead | Tiêu chuẩn đo lường, phân cấp rủi ro P0-P3, 4 phương pháp kiểm chứng (AST, Chaos, Probing) và ma trận RACI 9 vị trí. |
| **02** | **Quy Trình 5 Cổng Diễn Tập Trước Giờ G** | [REVIEW_PRE-FLIGHT STAGE GATES.md](../../.workflows/reviews/REVIEW_PRE-FLIGHT%20STAGE%20GATES.md) | DevOps & QA Engineering Lead | Điều phối diễn tập tuần tự qua 5 Stage Gates: Chuẩn mã nguồn $\rightarrow$ Kiểm thử kháng lỗi $\rightarrow$ Chất lượng AI $\rightarrow$ Dữ liệu $\rightarrow$ Sức chịu tải E2E. |
| **03** | **Quản Trị Giám Sát Sau Triển Khai (Hypercare)** | [REVIEW_HYPERCARE & INCIDENT ESCALATION.md](../../.workflows/reviews/REVIEW_HYPERCARE%20&%20INCIDENT%20ESCALATION.md) | Incident Commander & Principal SRE | Khung điều hành giám sát tăng cường 7 ngày (`T+15m`, `T+1h`, `T+24h`, `T+7d`), SLA phản hồi On-call và quy trình leo thang sự cố P0/P1. |
| **04** | **Biên Bản Nghiệm Thu & Phê Chuẩn Go-Live** | [REVIEW_SIGN-OFF & GO-LIVE APPROVAL.md](../../.workflows/reviews/REVIEW_SIGN-OFF%20&%20GO-LIVE%20APPROVAL.md) | Hội Đồng Đánh Giá (Release Board) | Tổng kết điểm số, bảng quyết định phát hành (Go / Waiver / No-Go) và thủ tục ký duyệt của 9 đại diện bộ phận. |

---

*Tài liệu này là Bộ chỉ huy trung tâm (Master Orchestrator). Mọi thay đổi về tiêu chí kiểm định bắt buộc phải cập nhật đồng bộ tại các tệp chuyên biệt trong thư mục [.workflows/reviews/](../../.workflows/reviews/) trước khi đệ trình Hội đồng Đánh giá.*
