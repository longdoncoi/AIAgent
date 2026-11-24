# REVIEW_SRE & DISASTER RECOVERY.md — Trụ Cột 10: Vận Hành, SRE Runbook & Khôi Phục Thảm Họa

> **Tài liệu kiểm định chuyên biệt (Production Readiness Review - Pillar 10 / 14)**  
> **Đơn vị phụ trách**: Incident Commander & Lead SRE Engineer  
> **Tài liệu tham chiếu**: [REVIEW.md](../../.workflows/reviews/REVIEW.md) · [ARCHITECTURE.md](../../Architecture.md) · [AGENTS.md](../../AGENTS.md)

---

## 1. Mục Tiêu & Phạm Vi Kiểm Định

Trụ cột này bảo đảm sự sẵn sàng của các quy trình vận hành và khôi phục sự cố thảm họa (Disaster Recovery):
1. **Năng Lực Rollback Khẩn Cấp**: Đảm bảo quy trình hoàn nguyên phiên bản diễn ra trong thời gian $< 3$ phút mà không làm gián đoạn kết nối.
2. **Khôi Phục Trạng Thái Cơ Sở Dữ Liệu (PITR)**: Kích hoạt WAL Archiving cho PostgreSQL, bảo đảm khôi phục dữ liệu về bất kỳ thời điểm nào.
3. **Sao Lưu Độc Lập Vector Store**: Cơ chế Snapshot định kỳ của Qdrant bảo toàn tri thức nạp vào hệ thống.
4. **SRE Runbook Khắc Phục Sự Cố**: Đầy đủ các tài liệu hướng dẫn xử lý sự cố chuẩn hóa (SOPs) cho GPU failure, DB lockup, và Pod OOM.

---

## 2. Danh Mục Checklist Kiểm Tra Chi Tiết

| ID | Trạng Thái | Mức Độ | Tiêu Chí Kiểm Tra (Verification Criteria) | Tệp Mã Nguồn / Cấu Hình | Phương Pháp & Lệnh Kiểm Chứng |
| :---: | :---: | :---: | :--- | :--- | :--- |
| **10.1** | `[ ]` | **P0** | **Quy Trình Hoàn Nguyên Khẩn Cấp (Emergency Rollback Runbook)**: Có runbook rollback chi tiết: Rollback GitOps commit qua ArgoCD CLI hoặc `kubectl rollout undo`, thời gian hoàn nguyên $< 3$ phút. | `docs/runbooks/rollback.md` | Diễn tập rollback trên môi trường Staging. |
| **10.2** | `[ ]` | **P0** | **Khôi Phục Dữ Liệu Điểm Thời Gian (PostgreSQL WAL PITR)**: PostgreSQL production bật chế độ WAL Archiving, sao lưu liên tục về S3/GCS. Đã diễn tập kịch bản khôi phục (Point-In-Time Recovery) với RPO $= 0$. | `deploy/db/postgresql.conf` | Kiểm tra WAL archiving process trong container DB. |
| **10.3** | `[ ]` | **P1** | **Sao Lưu Snapshot Định Kỳ Vector Store (Qdrant Snapshot)**: Lập lịch snapshot định kỳ hàng ngày cho collection `enterprise_knowledge` trên Qdrant và lưu trữ an toàn ngoài vùng khả dụng (multi-region). | `deploy/scripts/backup_qdrant.sh` | Kiểm tra lệnh tạo snapshot của Qdrant:<br>`curl -X POST http://localhost:6333/collections/enterprise_knowledge/snapshots` |
| **10.4** | `[ ]` | **P1** | **Sổ Tay Xử Lý Sự Cố GPU (GPU Outage Runbook)**: Runbook chi tiết các bước xử lý khi vLLM GPU sập nguồn hoặc quá nhiệt: Cô lập node lỗi, chuyển traffic sang CPU server, thông báo on-call. | `docs/runbooks/gpu_failure.md` | Đánh giá tài liệu và diễn tập mô phỏng trên staging. |

---

## 3. Quy Trình Xác Minh & Lệnh Thực Thi (Pre-Flight Runbook)

### Bước 1: Diễn Tập Rollback Bằng ArgoCD CLI
```bash
# Giả lập rollback deployment về revision trước đó
kubectl rollout undo deployment/api-supervisor -n default

# Xác nhận trạng thái rollout thành công:
kubectl rollout status deployment/api-supervisor -n default --timeout=60s
```

### Bước 2: Kiểm Tra Trạng Thái WAL Archiving & Snapshot Vector Store
```bash
# Kiểm tra file snapshot Qdrant:
curl -s -X POST http://localhost:6333/collections/enterprise_knowledge/snapshots | jq .
```

### Bước 3: Diễn Tập Ngắt vLLM & Xác Minh CPU Fallback
```bash
pytest tests/unit/test_production_hardening.py -v -k "test_circuit_breaker"
```

---

## 4. Danh Mục Giám Sát Sau Triển Khai (Post-Deployment Hypercare)

Sau khi hệ thống vận hành chính thức:
- [ ] **T+24 Giờ**: Xác nhận bản backup snapshot tự động đầu tiên của PostgreSQL WAL và Qdrant collection đã được tạo và lưu trữ an toàn trên object storage độc lập.
- [ ] **T+7 Ngày**: Thực hiện diễn tập khôi phục thảm họa định kỳ (DR Simulation) từ bản backup snapshot trên môi trường staging; kiểm chứng thời gian khôi phục dịch vụ (RTO $< 15$ phút) và mục tiêu điểm khôi phục (RPO $= 0$).

---

## 5. Trạng Thái Nghiệm Thu & Liên Kết Ký Duyệt

- **Tổng số tiêu chí kiểm định**: 4 (P0: 2, P1: 2, P2: 0)
- **Đạt**: _____ / 4
- **Kết luận**: [ ] ĐẠT (PASS) &nbsp;&nbsp;&nbsp; [ ] CẦN KHẮC PHỤC (REJECT)

> 🎯 **Biên Bản Ký Duyệt**: Kết quả đánh giá của Trụ cột này được tổng hợp và ký duyệt chính thức tại [Bảng Chữ Ký Phê Chuẩn Của Hội Đồng Đánh Giá (Mục 6.3 - REVIEW.md)](../../.workflows/reviews/REVIEW.md#63-bảng-chữ-ký-phê-chuẩn-của-hội-đồng-đánh-giá).
