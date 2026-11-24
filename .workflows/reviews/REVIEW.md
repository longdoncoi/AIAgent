# REVIEW.md — Cẩm Nang & Checklist Đánh Giá Độ Sẵn Sàng Production (PRR)

> **Tài liệu kiểm định kỹ thuật chính thức cấp quản lý** cho Hội đồng Đánh giá Phát hành (Release Board).  
> **Dự án**: `agentic-platform` (Multi-Agent Doanh Nghiệp Phân Cấp: LangGraph · vLLM · llama.cpp · LlamaIndex · MCP · A2A · Temporal · Kubernetes)  
> **Phiên bản tài liệu**: `v1.0.0-PROD-AUDIT` · **Tài liệu tham chiếu cơ sở**: [Architecture.md](file:///f:/PROJECTS/AIAgent/Architecture.md) · [AGENTS.md](file:///f:/PROJECTS/AIAgent/AGENTS.md)

---

## 1. Tổng Quan Khung Đánh Giá PRR (Executive Overview)

Tài liệu này đóng vai trò **Bộ chỉ huy trung tâm (Master Orchestrator)** điều phối toàn bộ tiêu chuẩn đo lường và quy trình kiểm định trước khi kích hoạt phát hành Production.  
Toàn bộ hệ thống kiểm định gồm **14 Trụ cột Kỹ thuật** và **4 Khung Quản trị Vận hành** được quản lý độc lập tại thư mục [.workflows/reviews/](file:///f:/PROJECTS/AIAgent/.workflows/reviews/):
- **Điều kiện Go-Live tối thiểu**: 100% tiêu chí P0 đạt (`44/44`), $\ge 90\%$ tiêu chí P1 đạt (`25/27`), Điểm tổng thể $\ge 95.0\%$. Chỉ cần **1 lỗi P0** chưa giải quyết $\rightarrow$ **Lập tức No-Go** (không ngoại lệ).

---

## 2. Bảng Tóm Tắt Trạng Thái Sẵn Sàng (Executive Readiness Dashboard)

| STT | Trụ Cột Kỹ Thuật Đánh Giá | Tệp Đặc Tả Chuyên Biệt | Đơn Vị Phụ Trách | Số Tiêu Chí (P0/P1/P2) | Trọng Số | Trạng Thái P0 |
| :---: | :--- | :--- | :--- | :---: | :---: | :---: |
| **01** | **Kiến Trúc & Giao Thức** | [REVIEW_ARCHITECTURE & PROTOCOLS.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_ARCHITECTURE%20&%20PROTOCOLS.md) | Lead Architect & Backend Lead | 6 (4 P0 / 2 P1) | 8% | [ ] 4/4 P0 |
| **02** | **Bộ Nhớ & Dữ Liệu** | [REVIEW_STATE, MEMORY & PERSISTENCE.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_STATE,%20MEMORY%20&%20PERSISTENCE.md) | Lead Backend & DBA | 6 (3 P0 / 3 P1) | 8% | [ ] 3/3 P0 |
| **03** | **Bộ Đệm Đa Tầng** | [REVIEW_MULTI-TIER CACHING SUBSYSTEM.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_MULTI-TIER%20CACHING%20SUBSYSTEM.md) | Performance & Backend Lead | 4 (2 P0 / 2 P1) | 7% | [ ] 2/2 P0 |
| **04** | **Bảo Mật & HITL** | [REVIEW_SECURITY, GUARDRAILS & HITL.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_SECURITY,%20GUARDRAILS%20&%20HITL.md) | SecOps / Cyber Security Lead | 6 (5 P0 / 1 P1) | 10% | [ ] 5/5 P0 |
| **05** | **Cổng Mô Hình (LLM)** | [REVIEW_MODEL GATEWAY & INFERENCE.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_MODEL%20GATEWAY%20&%20INFERENCE.md) | AI Serving / Infra Lead | 4 (2 P0 / 2 P1) | 8% | [ ] 2/2 P0 |
| **06** | **Chất Lượng Tác Tử (AI)**| [REVIEW_QUALITY GATES - DEEPEVAL & RAGAS.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_QUALITY%20GATES%20-%20DEEPEVAL%20&%20RAGAS.md) | AI Evaluation & Data Quality | 5 (3 P0 / 2 P1) | 10% | [ ] 3/3 P0 |
| **07** | **Kubernetes & SRE** | [REVIEW_KUBERNETES & SRE.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_KUBERNETES%20&%20SRE.md) | Principal SRE & Cloud Architect | 7 (5 P0 / 2 P1) | 8% | [ ] 5/5 P0 |
| **08** | **Giám Sát & Cảnh Báo** | [REVIEW_OBSERVABILITY & ALERTING.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_OBSERVABILITY%20&%20ALERTING.md) | SRE Monitoring & Backend Lead | 4 (2 P0 / 2 P1) | 7% | [ ] 2/2 P0 |
| **09** | **CI/CD & GitOps** | [REVIEW_CI-CD & GITOPS.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_CI-CD%20&%20GITOPS.md) | DevOps / Release Lead | 5 (4 P0 / 1 P1) | 5% | [ ] 4/4 P0 |
| **10** | **Vận Hành & Khôi Phục** | [REVIEW_SRE & DISASTER RECOVERY.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_SRE%20&%20DISASTER%20RECOVERY.md) | Incident Commander & Lead SRE | 4 (2 P0 / 2 P1) | 5% | [ ] 2/2 P0 |
| **11** | **Chiến Lược Test & QA** | [REVIEW_TESTING & QA STRATEGY.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_TESTING%20&%20QA%20STRATEGY.md) | QA Lead & Principal Tester | 6 (4 P0 / 2 P1) | 8% | [ ] 4/4 P0 |
| **12** | **Pháp Lý & Đạo Đức AI**| [REVIEW_COMPLIANCE, PRIVACY & AI ETHICS.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_COMPLIANCE,%20PRIVACY%20&%20AI%20ETHICS.md) | Legal Counsel & DPO Lead | 6 (4 P0 / 2 P1) | 6% | [ ] 4/4 P0 |
| **13** | **Quản Trị Chi Phí (FinOps)**| [REVIEW_FINOPS & COST GOVERNANCE.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_FINOPS%20&%20COST%20GOVERNANCE.md) | FinOps Lead & Cloud Architect | 5 (2 P0 / 2 P1 / 1 P2) | 5% | [ ] 2/2 P0 |
| **14** | **Quản Trị Tri Thức RAG**| [REVIEW_DATA GOVERNANCE & RAG RBAC.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_DATA%20GOVERNANCE%20&%20RAG%20RBAC.md) | Data Governance & RAG Lead | 5 (2 P0 / 2 P1 / 1 P2) | 5% | [ ] 2/2 P0 |
| **TỔNG**| **14 TRỤ CỘT KỸ THUẬT**| **14 TỆP CHUYÊN BIỆT** | **HỘI ĐỒNG THẨM ĐỊNH (9 LEADS)** | **73 TIÊU CHÍ (44 P0 / 27 P1 / 2 P2)**| **100%** | **[ ] 44/44 P0** |

---

## 3. Khung Quy Trình Quản Trị, Diễn Tập & Nghiệm Thu (Governance & Operational Domains)

Bên cạnh 14 trụ cột kỹ thuật, quy trình kiểm định và vận hành phát hành được chuẩn hóa thành 4 domain nghiệp vụ chuyên biệt:

| STT | Domain Quản Trị & Vận Hành | Tài Liệu Chi Tiết | Đơn Vị Chủ Trì | Nội Dung Trọng Tâm & Nhiệm Vụ |
| :---: | :--- | :--- | :--- | :--- |
| **01** | **Quy Chuẩn Kiểm Định & Phân Quyền RACI** | [REVIEW_GOVERNANCE & AUDIT STANDARDS.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_GOVERNANCE%20&%20AUDIT%20STANDARDS.md) | Chief AI Architect & SecOps Lead | Tiêu chuẩn đo lường, phân cấp rủi ro P0-P3, 4 phương pháp kiểm chứng (AST, Chaos, Probing) và ma trận RACI 9 vị trí. |
| **02** | **Quy Trình 5 Cổng Diễn Tập Trước Giờ G** | [REVIEW_PRE-FLIGHT STAGE GATES.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_PRE-FLIGHT%20STAGE%20GATES.md) | DevOps & QA Engineering Lead | Điều phối diễn tập tuần tự qua 5 Stage Gates: Chuẩn mã nguồn $\rightarrow$ Kiểm thử kháng lỗi $\rightarrow$ Chất lượng AI $\rightarrow$ Dữ liệu $\rightarrow$ Sức chịu tải E2E. |
| **03** | **Quản Trị Giám Sát Sau Triển Khai (Hypercare)** | [REVIEW_HYPERCARE & INCIDENT ESCALATION.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_HYPERCARE%20&%20INCIDENT%20ESCALATION.md) | Incident Commander & Principal SRE | Khung điều hành giám sát tăng cường 7 ngày (`T+15m`, `T+1h`, `T+24h`, `T+7d`), SLA phản hồi On-call và quy trình leo thang sự cố P0/P1. |
| **04** | **Biên Bản Nghiệm Thu & Phê Chuẩn Go-Live** | [REVIEW_SIGN-OFF & GO-LIVE APPROVAL.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_SIGN-OFF%20&%20GO-LIVE%20APPROVAL.md) | Hội Đồng Đánh Giá (Release Board) | Tổng kết điểm số, bảng quyết định phát hành (Go / Waiver / No-Go) và thủ tục ký duyệt của 9 đại diện bộ phận. |

---

*Tài liệu này là Bộ chỉ huy trung tâm (Master Orchestrator). Mọi thay đổi về tiêu chí kiểm định bắt buộc phải cập nhật đồng bộ tại các tệp chuyên biệt trong thư mục [.workflows/reviews/](file:///f:/PROJECTS/AIAgent/.workflows/reviews/) trước khi đệ trình Hội đồng Đánh giá.*

---

## 6. Bằng Chứng Kiểm Định Tự Động & Phê Chuẩn (Auto-Evidence & Sign-off)

### 6.1 Trạng Thái Bằng Chứng

> **AUTO-EVIDENCE PENDING** — **Chờ đối soát và ký bởi Release Board.** Không có trạng thái `PASSED` nào được tự động gán.

- Artifact máy tạo: **[evidence/prr/latest.json](file:///f:/PROJECTS/AIAgent/evidence/prr/latest.json)** — chứa `commit_sha`, `timestamp_utc`, exit code và output của từng lệnh `ruff`, `mypy`, `pytest --collect-only`, `eval --mode smoke`, parse YAML K8s.
- Lệnh sinh artifact: `python scripts/generate_prr_evidence.py` (hoặc `make prr-evidence`). Script **không** chạy full pytest; verdict luôn là `AUTO-EVIDENCE PENDING REVIEW BOARD`.
- CI upload artifact này dưới tên `prr-evidence-<sha>` và job phát hành tải lại đúng artifact của workflow hiện tại; bản `evidence/prr/latest.json` đang được track trong Git không được dùng thay thế. Pipeline **không** tự động deploy nếu thiếu artifact sign-off (xem `evidence/signoff/release-board.json` và bước `Verify Release Sign-off` trong `.github/workflows/ci.yml`).
- Release gate chất lượng là lệnh riêng `make eval-release` (`evals/release_gate.py`), **không** phải smoke eval. Chỉ release gate mới được phép đưa ra kết luận "Ready for Production", và nó từ chối mọi proxy/heuristic.

### 6.2 Các Tiêu Chí Chưa Có Code (Truthful Gaps)

Các hạng mục dưới đây được ghi rõ trạng thái triển khai và phần bằng chứng vận hành còn thiếu thay vì để trống — Hội đồng cần nhìn đúng thực trạng:

| Tiêu chí | Tệp review | Trạng thái thực tế |
| :--- | :--- | :--- |
| Xóa dữ liệu theo yêu cầu (Right to be Forgotten) | `REVIEW_COMPLIANCE, PRIVACY & AI ETHICS.md` (12.1) | **IMPLEMENTED (code + test), chờ diễn tập staging** — purge fail-closed theo owner trên checkpoint/Redis/LTM, đồng thời xóa Qdrant, Knowledge Graph, cache và raw files theo tenant. |
| Citations có cấu trúc (`doc_id`, `page`, `source`) | `REVIEW_COMPLIANCE, PRIVACY & AI ETHICS.md` (12.4) | **IMPLEMENTED (code + test), chờ release eval** — `search_policy` phát block `[CITATIONS]`; Finalize node chỉ tổng hợp citation đã xác minh và từ chối trả lời RAG thiếu nguồn. |
| RBAC tài liệu theo `user_roles` / `department` | `REVIEW_DATA GOVERNANCE & RAG RBAC.md` (14.1) | **IMPLEMENTED (code + test), chờ kiểm chứng staging** — claim JWT được pin vào execution context ký A2A; Qdrant pushdown và lớp lọc phòng thủ áp dụng tenant, role, department, ACL; caller không thể override bằng tool arguments. |
| Phân tách approval signer và workload secrets | `REVIEW_SECURITY, GUARDRAILS & HITL.md` (4.3/4.6) | **IMPLEMENTED (code + manifest + regression), chờ probing staging** — Supervisor/API giữ Ed25519 private key; L3/L4 chỉ giữ public key và attenuate/verify capability. ExternalSecret tách riêng API / Chat / RAG / Coding / MCP / maintenance / database; production role verifier fail startup nếu nhận private key. |
| Ngân sách token cứng theo tenant + cảnh báo đột biến | `REVIEW_FINOPS & COST GOVERNANCE.md` (13.1, 13.5) | **CONFIGURED — cần kiểm chứng runtime**. Cost-tag `client_id/department/env` đã phát metric `agent_cost_allocation_total`. |
| WAL PITR với RPO = 0 | `REVIEW_SRE & DISASTER RECOVERY.md` (10.2) | **PARTIAL / honest gap** — chỉ có logical backup CronJob (`deploy/k8s/backup.yaml`). PITR RPO=0 cần WAL archiving + S3/GCS do nhà cung cấp DB quản lý. |

### 6.2.1 Bản Ghi Đợt Gia Cố P0 (2026-11-24)

Đợt gia cố đã xử lý các blocker Critical (có regression test trong `tests/unit/test_*_v3.py`):
ENVIRONMENT fail-open; HITL grant forgeable + wildcard owner + replay; xác minh grant một lần tại
ranh giới MCP và ràng buộc exact arguments; nhánh `approval_granted` fail-open; SSE/A2A output chưa
sanitize; guardrail double-encoding; tenant/RBAC override qua tool argument; cô lập LTM theo tenant;
RTBF mở rộng qua vector/KG/cache/raw files; cache invalidation không xóa L2; symlink escape trong repo
tool; rate limiter production fail-open; MCP probe/pgBouncer/Ingress/OTLP blockers; release gate không
nằm trong CI; sign-off gate chỉ kiểm tra file tồn tại; action/base image chưa pin immutable digest.

Kết quả code-level tại workspace ngày 2026-11-25: Ruff và mypy strict đạt; `383 passed, 1 skipped`,
coverage gần nhất `87.70%`; audit dependency production theo lockfile không phát hiện CVE đã biết. Strict release gate vẫn
**FAIL** vì không có LiteLLM/LLM judge thật đang reachable trong môi trường kiểm định này.

> **CẦN HIỂU RÕ**: đây là bằng chứng **code-level**. Nó KHÔNG thay cho việc chạy
> `python evals/release_gate.py` với LLM judge/neural model thật, diễn tập DR/load test trên
> cụm K8s thật, và 9 chữ ký của Release Board. Verdict tự động vẫn là
> `AUTO-EVIDENCE PENDING REVIEW BOARD`.

### 6.3 Bảng Chữ Ký Phê Chuẩn Của Hội Đồng Đánh Giá (Release Board)

> **9 ô chữ ký dưới đây để trống — chờ ký bởi Release Board.** Không được tự động điền tên, ngày hay "PASSED".

| # | Vai Trò (RACI) | Đại Diện | Quyết Định (Go / Waiver / No-Go) | Chữ Ký | Ngày (UTC) |
| :---: | :--- | :--- | :---: | :---: | :---: |
| 1 | Chief AI Architect (A) | Lead Architect | ____________ | ____________ | ____________ |
| 2 | Lead AI/Backend Engineer (R) | Backend Tech Lead | ____________ | ____________ | ____________ |
| 3 | QA / Test Engineering Lead (R/A) | Head of QA | ____________ | ____________ | ____________ |
| 4 | SecOps / Security Lead (C) | Head of Cyber Security | ____________ | ____________ | ____________ |
| 5 | Legal Counsel & DPO (C/A) | Legal & DPO Lead | ____________ | ____________ | ____________ |
| 6 | FinOps / Cloud Cost Lead (C) | FinOps Lead | ____________ | ____________ | ____________ |
| 7 | Data Governance Lead (R) | Data Governance Lead | ____________ | ____________ | ____________ |
| 8 | Principal SRE / DevOps (R/A) | Principal SRE | ____________ | ____________ | ____________ |
| 9 | Product Owner (I) | Product Manager | ____________ | ____________ | ____________ |

*Chuyển tiếp theo giao thức hai commit: Hội đồng ký source commit `C`; tạo commit attestation `A` chỉ thêm/sửa `evidence/signoff/release-board.json` với `commit_sha=C`; sau đó mới tạo release tag trỏ vào `A`. CI bắt buộc `parent(A)=C`, diff của `A` chỉ có đúng tệp biên bản, và PRR artifact phải thuộc `A`. Nếu có bất kỳ thay đổi code/config nào sau `C`, phải hủy attestation và tái thẩm định từ commit nguồn mới.*
