# REVIEW_GOVERNANCE & AUDIT STANDARDS.md — Quy Chuẩn Kiểm Định, Phân Cấp Rủi Ro & Ma Trận Phân Quyền RACI

> **Tài liệu kiểm định chuyên biệt (Production Readiness Review - Governance Domain 01 / 04)**  
> **Đơn vị phụ trách**: Chief AI Architect & SecOps / Cyber Security Lead  
> **Tài liệu tham chiếu**: [REVIEW.md](../../.workflows/reviews/REVIEW.md) · [ARCHITECTURE.md](../../Architecture.md) · [AGENTS.md](../../AGENTS.md)

---

## 1. Mục Tiêu & Tầm Nhìn Kiểm Định Cấp Chiến Lược

Tài liệu này xác lập bộ tiêu chuẩn đo lường và quy chuẩn kiểm định bắt buộc trước khi kích hoạt quy trình phát hành hệ thống `agentic-platform` lên môi trường **Production**. Bốn mục tiêu chiến lược cốt lõi:

1. **Bảo đảm tính toàn vẹn kiến trúc (Architectural Invariants)**: Không phá vỡ quy tắc phân tầng một chiều, giao thức chuẩn hóa (A2A, FastMCP) và tính phi trạng thái (stateless) của các Pods.
2. **Triệt tiêu rủi ro an ninh & Zero-Trust (Zero-Trust Security & Guardrails)**: Đảm bảo không tồn tại secret mặc định, ngăn chặn 100% prompt injection, bảo vệ dữ liệu nhạy cảm PII, và sandbox cách ly hoàn toàn các lệnh thực thi nhạy cảm.
3. **Đạt chuẩn chất lượng AI & Giảm thiểu ảo giác (Quality Gates & Groundedness)**: Đảm bảo độ chính xác điều phối tác tử, độ căn cứ tài liệu, độ trung thực và loại trừ ảo giác theo tiêu chuẩn DeepEval và RAGAS framework.
4. **Độ tin cậy, Khả năng chịu lỗi & SRE (High Availability & SRE Resilience)**: Đảm bảo khả năng chịu lỗi, tự phục hồi, failover mô hình GPU sang CPU, sao lưu dữ liệu và hệ thống giám sát cảnh báo tức thời.

---

## 2. Phân Cấp Mức Độ Nghiêm Trọng (Severity Classification) & Điều Kiện Nghiệm Thu

| Mức Độ (Severity) | Định Nghĩa & Tác Động Hệ Thống | Điều Kiện Nghiệm Thu Go-Live |
| :---: | :--- | :--- |
| **P0 (Blocker)** | **Vi phạm nguyên tắc bất biến (Inviolables)**: Lỗ hổng bảo mật nghiêm trọng, lộ secret mặc định, mất trạng thái phiên khi scale pod, chạy code nguy hiểm ngoài sandbox, sập hệ thống hoặc sai lệch dữ liệu. | **BẮT BUỘC 100% ĐẠT**. Chỉ cần **1 lỗi P0** chưa giải quyết $\rightarrow$ **LẬP TỨC NO-GO**. Không được phép có ngoại lệ (No Waiver). |
| **P1 (Critical)** | **Lỗi suy giảm nghiêm trọng**: Điểm Quality Gate dưới ngưỡng quy định, P99 latency vượt quá cam kết SLA, thiếu probes Kubernetes, thiếu Alertmanager rules, hoặc không thể failover GPU sang CPU. | **BẮT BUỘC KHẮC PHỤC**. Trường hợp đặc biệt cần ký duyệt văn bản ngoại lệ (Waiver Approval) từ cả Lead Architect và Head of Engineering. |
| **P2 (Major)** | **Suy giảm hiệu năng hoặc khiếm khuyết phụ**: Cache miss ratio cao hơn kỳ vọng, thiếu log context trong một số nghiệp vụ thứ cấp, tài liệu runbook chưa cập nhật đầy đủ một số kịch bản hiếm gặp. | **ĐƯỢC PHÉP GO-LIVE CÓ ĐIỀU KIỆN**. Phải tạo ticket JIRA ưu tiên xử lý trong vòng 1 sprint (7 ngày) sau phát hành. |
| **P3 (Minor)** | **Tối ưu hóa & Làm sạch**: Lỗi định dạng log nhỏ, cảnh báo deprecation không ảnh hưởng runtime, tài liệu hướng dẫn nội bộ cần trau chuốt thêm. | **CHO PHÉP BỎ QUA TRONG NGÀY GO-LIVE**. Đưa vào backlog kỹ thuật định kỳ. |

---

## 3. Quy Chuẩn Đánh Giá & Phương Pháp Kiểm Chứng (Audit Methodology)

Tất cả các tệp kiểm định chuyên biệt (`REVIEW_*.md`) áp dụng đồng nhất cấu trúc bảng kiểm tra gồm 6 trường thông tin:
- **ID**: Mã định danh duy nhất của tiêu chí kiểm tra (dạng `X.Y` với X là số thứ tự trụ cột).
- **Trạng Thái**: Đánh dấu `[ ]` (chưa đạt) hoặc `[x]` (đã nghiệm thu đạt).
- **Mức Độ**: Phân cấp rủi ro kỹ thuật (`P0`, `P1`, `P2`).
- **Tiêu Chí Kiểm Tra (Verification Criteria)**: Yêu cầu kỹ thuật bắt buộc phải thỏa mãn.
- **Tệp Mã Nguồn / Cấu Hình**: Đường dẫn tệp hoặc module chịu trách nhiệm trực tiếp.
- **Phương Pháp & Lệnh Kiểm Chứng**: Lệnh kiểm tra tự động hoặc phương thức xác minh khách quan.

### 4 Phương Pháp Kiểm Chứng Chuẩn Mực:
1. **Static AST & Import Verification**: Thẩm định mã nguồn tĩnh, kiểm tra cây cú pháp (AST) nhằm bảo đảm tính độc lập và quy tắc phân tầng một chiều.
2. **Dynamic Automated Testing**: Thực thi bộ kiểm thử tự động (Unit, Integration, Contract, E2E) đo lường độ bao phủ (Coverage) và tính toàn vẹn dữ liệu.
3. **Resilience & Chaos Simulation**: Mô phỏng sự cố ngắt quãng hạ tầng (mất kết nối cơ sở dữ liệu, sập bộ đệm, quá tải mô hình GPU) để kiểm tra khả năng tự phục hồi (Self-healing).
4. **Production Probing & Benchmarking**: Đo lường lưu lượng tải thực tế, độ trễ phân vị (P50/P90/P99), tỷ lệ trúng bộ đệm (Cache Hit Ratio) và mức tiêu thụ tài nguyên.

> [!CAUTION]
> **Nguyên Tắc An Toàn Môi Trường**: Tuyệt đối không chạy các lệnh xóa hoặc làm biến đổi dữ liệu phá hủy trực tiếp trên cơ sở dữ liệu Production. Mọi diễn tập khôi phục thảm họa hoặc kiểm thử tải phá hủy bắt buộc phải thực hiện trên môi trường Staging/Replication tương đương.

---

## 4. Ma Trận Trách Nhiệm Nghiệm Thu (RACI Matrix)

| Vai Trò | Trách Nhiệm Chính Trong Quy Trình PRR | Đại Diện Hội Đồng Ký Duyệt |
| :--- | :--- | :--- |
| **Chief AI Architect (A)** | Phê duyệt tổng thể tính toàn vẹn kiến trúc, nguyên tắc bất biến và quyết định Go / No-Go cuối cùng. | Lead Architect |
| **Lead AI/Backend Engineer (R)** | Thực thi kiểm thử, bảo đảm chất lượng mã nguồn, tích hợp A2A, FastMCP, LangGraph và LlamaIndex. | Backend Tech Lead |
| **QA / Test Engineering Lead (R/A)** | Nghiệm thu chiến lược kiểm thử, Code Coverage, Load/Stress test và kiểm thử kháng sự cố (Chaos Resilience). | Head of QA |
| **SecOps / Security Lead (C)** | Kiểm định chính sách bảo mật Zero-Trust, Quét lỗ hổng CVE, Guardrails, Sandbox, PII Redaction và duyệt HITL. | Head of Cyber Security |
| **Legal Counsel & DPO (C/A)** | Kiểm tra tuân thủ Nghị định 13/2023/NĐ-CP, GDPR, Zero Data Retention, Quyền được lãng quên, Bản quyền tri thức. | Legal & DPO Lead |
| **FinOps / Cloud Cost Lead (C)** | Đánh giá hạn mức ngân sách token, quota per tenant, GPU auto-scaling và chi phí vận hành hạ tầng đám mây. | FinOps Lead |
| **Data Governance Lead (R)** | Đảm bảo phân quyền tài liệu RAG RBAC, khử trùng lặp, thu hồi tri thức hết hạn và vệ sinh dữ liệu. | Data Governance Lead |
| **Principal SRE / DevOps (R/A)** | Nghiệm thu hạ tầng Kubernetes, Istio mTLS, NetworkPolicy, Prometheus/Grafana, CI/CD GitOps và quy trình DR. | Principal SRE |
| **Product Owner (I)** | Tiếp nhận trạng thái phát hành, xác nhận các tính năng nghiệp vụ và nghiệm thu phạm vi sản phẩm. | Product Manager |

*(Ghi chú: **R** = Responsible/Thực hiện; **A** = Accountable/Chịu trách nhiệm phê duyệt; **C** = Consulted/Tư vấn kiểm duyệt; **I** = Informed/Nhận thông báo)*
