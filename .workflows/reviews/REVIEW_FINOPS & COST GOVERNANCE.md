# REVIEW_FINOPS & COST GOVERNANCE.md — Trụ Cột 13: Quản Trị Chi Phí & Tối Ưu Hóa Tài Nguyên Đám Mây

> **Tài liệu kiểm định chuyên biệt (Production Readiness Review - Pillar 13 / 14)**  
> **Đơn vị phụ trách**: FinOps Lead & Cloud Cost Architect & AI Infrastructure Engineer  
> **Tài liệu tham chiếu**: [REVIEW.md](REVIEW.md) · [Architecture.md](../../Architecture.md) · [AGENTS.md](../../AGENTS.md) · [configs/litellm_config.yaml](../../configs/litellm_config.yaml)

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
| **13.1** | **CONFIGURED — runtime verification pending** | **P0** | **Kiểm Soát Hạn Mức Token Cứng (Hard Token Quota per Tenant)**: LiteLLM cấu hình `max_budget` và `budget_duration` cho từng API Key của tenant. Khi chạm ngưỡng 100% hạn mức, request bị từ chối với mã lỗi `429 Too Many Requests`. | [configs/litellm_config.yaml](../../configs/litellm_config.yaml) | Kiểm tra file cấu hình LiteLLM và test vượt hạn mức token. |
| **13.2** | **IMPLEMENTED — runtime verification pending** | **P0** | **Tự Động Thu Gom Checkpoint Cũ (Storage TTL & Compaction)**: Redis checkpoint có TTL mặc định 7 ngày và tenant-scoped cleanup; PostgreSQL có `created_at` do migration quản lý và CronJob hằng ngày xóa toàn bộ bảng checkpoint của thread không hoạt động quá 30 ngày. Backend unavailable thì fail closed. | [src/agentic/memory/redis_saver.py](../../src/agentic/memory/redis_saver.py)<br>[src/agentic/memory/data_lifecycle.py](../../src/agentic/memory/data_lifecycle.py)<br>[deploy/k8s/checkpoint-retention.yaml](../../deploy/k8s/checkpoint-retention.yaml) | Chạy regression test lifecycle; trên staging kiểm tra Redis TTL và log/audit của CronJob PostgreSQL. |
| **13.3** | **CONFIGURED — runtime verification pending** | **P1** | **Tự Động Scale-Down GPU Ngoài Giờ (GPU Autoscaling Schedule)**: KEDA `ScaledObject` giữ 4 replicas vLLM từ 06:00 đến 23:00 và giảm về minimum 1 replica từ 23:00 đến 06:00 theo `Asia/Ho_Chi_Minh`; lỗi trigger fallback về 1 replica. | [deploy/overlays/production/vllm-scheduled-scaling.yaml](../../deploy/overlays/production/vllm-scheduled-scaling.yaml) | Render production overlay, kiểm tra ScaledObject và xác nhận HPA/KEDA thực tế trên staging. |
| **13.4** | **IMPLEMENTED — runtime verification pending** | **P1** | **Gắn Thẻ Phân Bổ Chi Phí (Cost Allocation Tagging)**: Mọi request qua API Supervisor và LiteLLM Gateway đều đính kèm metadata đã xác thực: `client_id`, `department`, `environment`, `agent`; đồng thời phát metric phân bổ chi phí. Không fallback sang `user_id` để tránh cardinality theo người dùng. | [src/agentic/core/execution_context.py](../../src/agentic/core/execution_context.py)<br>[src/agentic/llm/gateway.py](../../src/agentic/llm/gateway.py) | Gửi request và kiểm tra `metadata.spend_logs_metadata` trong LiteLLM spend log cùng metric `agent_cost_allocation_total`. |
| **13.5** | **CONFIGURED — runtime verification pending** | **P2** | **Cảnh Báo Tiêu Hao Đột Biến (FinOps Anomaly Alerting)**: Alertmanager cấu hình rule `TokenSpikeAnomalyAlert`: Khi tổng số token tiêu thụ trong 15 phút tăng gấp đôi so với cùng kỳ tuần trước, kích hoạt thông báo kênh Slack FinOps. | [deploy/monitoring/alerts.yaml](../../deploy/monitoring/alerts.yaml) | Kiểm tra cú pháp rule trong `alerts.yaml`. |

### Thực Trạng Triển Khai (Implementation Truth)

- **13.1 — CONFIGURED — runtime verification pending**: `configs/litellm_config.yaml` đã có `max_budget` / `budget_duration` (global + từng alias). Chưa có bằng chứng runtime rằng gateway từ chối request ở 100% hạn mức; cần test với key thật trước khi ký duyệt.
- **13.2 — IMPLEMENTED, runtime verification pending**: Redis dùng TTL trên record/index; PostgreSQL migration `002_checkpoint_retention` bổ sung timestamp/index và CronJob gọi cleanup parameterized theo stale thread. Cần chạy migration + CronJob trên staging và đối soát dung lượng trước/sau.
- **13.3 — CONFIGURED, runtime verification pending**: production overlay khai báo KEDA cron scaler có timezone và giới hạn `1..4`; cần KEDA operator trên cluster và đo số replica qua hai biên thời gian trước khi ký.
- **13.4 — IMPLEMENTED, runtime verification pending**: claim `client_id`/`department` được truyền qua A2A execution context; Gateway ghi các dimension được bảo vệ vào LiteLLM `spend_logs_metadata` và metric `agent_cost_allocation_total`. Giá trị caller trùng tên bị ghi đè bởi context đã xác thực. Cần đối soát spend log/Prometheus trên staging trước khi ký.
- **13.5 — CONFIGURED — runtime verification pending**: rule `TokenSpikeAnomalyAlert` đã thêm vào `deploy/monitoring/alerts.yaml`; chưa có bằng chứng Alertmanager/Slack đã nhận rule và gửi thông báo thật.

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

> 🎯 **Biên Bản Ký Duyệt**: Kết quả đánh giá của Trụ cột này được tổng hợp và ký duyệt chính thức tại [Bảng Chữ Ký Phê Chuẩn Của Hội Đồng Đánh Giá (Mục 6.3 - REVIEW.md)](REVIEW.md#63-bảng-chữ-ký-phê-chuẩn-của-hội-đồng-đánh-giá).

---

## 6. Bằng Chứng Kiểm Định Tự Động (Auto-Evidence Status)

> **AUTO-EVIDENCE PENDING** — chờ đối soát và **ký bởi Release Board**. Không có nhãn `PASSED` được tự động gán cho tài liệu này.

- Artifact: [evidence/prr/latest.json](../../evidence/prr/latest.json) (`commit_sha`, `timestamp_utc`, exit code của `ruff` / `mypy --no-incremental` / `pytest -n auto --cov=src --cov-fail-under=85` / `eval --mode smoke` / `red_teaming_runner` / `restore_drill --smoke` / parse YAML).
- Sinh artifact: `python scripts/generate_prr_evidence.py`. Tất cả ô trạng thái `[]` ở các mục trên vẫn giữ nguyên cho đến khi Hội đồng đối soát artifact và ký duyệt.
- Bảng chữ ký 9 đại diện: [REVIEW.md mục 6.3](REVIEW.md#63-bảng-chữ-ký-phê-chuẩn-của-hội-đồng-đánh-giá).
