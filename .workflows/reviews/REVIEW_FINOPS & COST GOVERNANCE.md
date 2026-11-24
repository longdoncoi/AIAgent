# REVIEW_FINOPS & COST GOVERNANCE.md — Trụ Cột 13: Quản Trị Chi Phí & Tối Ưu Hóa Tài Nguyên Đám Mây

> **Tài liệu kiểm định chuyên biệt (Production Readiness Review - Pillar 13 / 14)**  
> **Đơn vị phụ trách**: FinOps Lead & Cloud Cost Architect & AI Infrastructure Engineer  
> **Tài liệu tham chiếu**: [REVIEW.md](../../.workflows/reviews/REVIEW.md) · [ARCHITECTURE.md](../../Architecture.md) · [AGENTS.md](../../AGENTS.md) · [configs/litellm_config.yaml](../../configs/litellm_config.yaml)

---

## 1. Mục Tiêu & Phạm Vi Kiểm Định

Trụ cột này bảo đảm hệ thống vận hành bền vững về mặt kinh tế, kiểm soát chặt chẽ chi phí mô hình và tài nguyên hạ tầng:
1. **Kiểm Soát Ngân Sách Token (Token Budgeting & Rate Limiting)**: Thiết lập quota cứng theo từng phòng ban/tenant, tránh tình trạng một nhóm người dùng sử dụng quá mức làm cạn kiệt hạn mức.
2. **Tối Ưu Hóa Tự Động Hạ Tầng GPU (Scale-to-Zero & Spot Instances)**: Cấu hình tự động giảm số lượng GPU instances hoặc chuyển sang Spot instances ngoài giờ cao điểm (22h - 6h), tiết kiệm tới 40-60% chi phí điện toán đám mây.
3. **Phân Bổ Chi Phí Minh Bạch (Cost Allocation Tagging)**: Mọi request gửi tới Gateway phải được gắn nhãn `tenant_id`, `department`, và `project_code` để lập báo cáo tài chính chính xác.
4. **Vòng Đời Lưu Trữ Tiết Kiệm (Storage Lifecycle & Compaction)**: Tự động nén và đẩy checkpoints/logs cũ ($> 30$ ngày) từ PostgreSQL/Redis sang S3/GCS Glacier Cold Storage.
5. **Cảnh Báo Vượt Ngưỡng Chi Phí (FinOps Anomaly Alerting)**: Tích hợp cảnh báo Prometheus/Slack tức thời khi chi phí token theo giờ tăng đột biến vượt quá $200\%$ mức trung bình.

---

## 2. Danh Mục Checklist Kiểm Tra Chi Tiết

| ID | Trạng Thái | Mức Độ | Tiêu Chí Kiểm Tra (Verification Criteria) | Tệp Mã Nguồn / Cấu Hình | Phương Pháp & Lệnh Kiểm Chứng |
| :---: | :---: | :---: | :--- | :--- | :--- |
| **13.1** | `[ ]` | **P0** | **Kiểm Soát Hạn Mức Token Cứng (Hard Token Quota per Tenant)**: LiteLLM cấu hình `max_budget` và `budget_duration` cho từng API Key của tenant. Khi chạm ngưỡng 100% hạn mức, request bị từ chối với mã lỗi `429 Too Many Requests`. | [configs/litellm_config.yaml](../../configs/litellm_config.yaml) | Kiểm tra file cấu hình LiteLLM và test vượt hạn mức token. |
| **13.2** | `[ ]` | **P0** | **Tự Động Thu Gom Checkpoint Cũ (Storage TTL & Compaction)**: Bảng `checkpoints` trên PostgreSQL và Redis session keys có cơ chế TTL/Compaction định kỳ (mặc định xóa hoặc archive sang S3 sau 30 ngày), chống phình to dung lượng đĩa. | [src/agentic/memory/short_term.py](../../src/agentic/memory/short_term.py) | Kiểm tra Redis TTL của checkpoint keys:<br>`redis-cli TTL "checkpoint:thread_id_test"` |
| **13.3** | `[ ]` | **P1** | **Tự Động Scale-Down GPU Ngoài Giờ (GPU Autoscaling Schedule)**: HPA hoặc CronJob Kubernetes cấu hình scale giảm số replicas của vLLM Pods từ 4 xuống 1 trong khung giờ từ 23h đến 6h sáng hôm sau. | [deploy/k8s/deployment.yaml](../../deploy/k8s/deployment.yaml) | Review manifest CronJob hoặc KEDA scaledobject. |
| **13.4** | `[ ]` | **P1** | **Gắn Thẻ Phân Bổ Chi Phí (Cost Allocation Tagging)**: Mọi request qua API Supervisor và LiteLLM Gateway đều đính kèm metadata: `client_id`, `department`, `env: production` trong trường metadata. | [src/agentic/llm/gateway.py](../../src/agentic/llm/gateway.py) | Gửi request và kiểm tra trường metadata lưu trong audit log và metrics. |
| **13.5** | `[ ]` | **P2** | **Cảnh Báo Tiêu Hao Đột Biến (FinOps Anomaly Alerting)**: Alertmanager cấu hình rule `TokenSpikeAnomalyAlert`: Khi tổng số token tiêu thụ trong 15 phút tăng gấp đôi so với cùng kỳ tuần trước, kích hoạt thông báo kênh Slack FinOps. | [deploy/monitoring/alerts.yaml](../../deploy/monitoring/alerts.yaml) | Kiểm tra cú pháp rule trong `alerts.yaml`. |

---

## 3. Quy Trình Xác Minh & Lệnh Thực Thi (Pre-Flight Runbook)

### Bước 1: Kiểm Tra Giới Hạn Token Trong Gateway
```bash
# Kiểm tra cấu hình budget trong litellm_config.yaml
grep -A 5 "budget" configs/litellm_config.yaml
```

### Bước 2: Kiểm Tra TTL Trên Redis Checkpointer
```bash
# Kiểm tra giá trị TTL của các session keys trên Redis:
# Giá trị trả về phải > 0 và <= 604800 (7 ngày)
redis-cli TTL "checkpoint:thread_id_dry_run"
```

### Bước 3: Đo Lường Tổng Số Token Tiêu Thụ Phân Bổ Theo Mô Hình
```bash
curl -s http://localhost:8000/metrics | grep "agent_tokens_total"
```

---

## 4. Danh Mục Giám Sát Sau Triển Khai (Post-Deployment Hypercare)

Sau khi hệ thống vận hành chính thức:
- [ ] **T+24 Giờ**: Đối soát tổng lượng token thực tế tiêu thụ so với hạn mức Token Budget đã cấp phát theo tenant.
- [ ] **T+24 Giờ**: Kiểm tra cơ chế tự động scale-down GPU ngoài giờ làm việc đã kích hoạt chính xác để tiết kiệm chi phí.
- [ ] **T+7 Ngày**: Đối soát tổng chi phí hạ tầng thực tế (GPU instance, CPU cluster, Vector DB, Bandwidth) so với mô hình dự toán tài chính FinOps.

---

## 5. Trạng Thái Nghiệm Thu & Liên Kết Ký Duyệt

- **Tổng số tiêu chí kiểm định**: 5 (P0: 2, P1: 2, P2: 1)
- **Đạt**: _____ / 5
- **Kết luận**: [ ] ĐẠT (PASS) &nbsp;&nbsp;&nbsp; [ ] CẦN KHẮC PHỤC (REJECT)

> 🎯 **Biên Bản Ký Duyệt**: Kết quả đánh giá của Trụ cột này được tổng hợp và ký duyệt chính thức tại [Bảng Chữ Ký Phê Chuẩn Của Hội Đồng Đánh Giá (Mục 6.3 - REVIEW.md)](../../.workflows/reviews/REVIEW.md#63-bảng-chữ-ký-phê-chuẩn-của-hội-đồng-đánh-giá).
