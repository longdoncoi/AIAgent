# REVIEW_SRE & DISASTER RECOVERY.md — Trụ Cột 10: Vận Hành, SRE Runbook & Khôi Phục Thảm Họa

> **Tài liệu kiểm định chuyên biệt (Production Readiness Review - Pillar 10 / 14)**  
> **Đơn vị phụ trách**: Incident Commander & Lead SRE Engineer  
> **Tài liệu tham chiếu**: [REVIEW.md](REVIEW.md) · [Architecture.md](../../Architecture.md) · [AGENTS.md](../../AGENTS.md)

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
| **10.2** | `[ ]` | **P0** | **Khôi Phục Dữ Liệu & Diễn Tập Restore Drill (PostgreSQL PITR + Script Kiểm Định)**: PostgreSQL production bật WAL Archiving về object storage (`RPO = 0`); kịch bản khôi phục và kiểm chứng SHA-256 + `verify_audit_chain` được chuẩn hóa qua `scripts/restore_drill.py` (`--smoke` trong CI và `--pg-dump` / `--qdrant-snapshot` trên staging). | `deploy/k8s/backup.yaml`<br>[scripts/restore_drill.py](../../scripts/restore_drill.py) | Chạy smoke drill & staging PITR:<br>`python scripts/restore_drill.py --smoke` |
| **10.3** | `[ ]` | **P1** | **Sao Lưu Snapshot Định Kỳ Vector Store (Qdrant Snapshot)**: CronJob `agentic-qdrant-backup` sao lưu định kỳ trên PVC `agentic-qdrant-backups` (streaming SHA-256 + offsite upload fail-closed + dọn 14 ngày) và diễn tập nạp lại qua `scripts/restore_drill.py --qdrant-snapshot`. | `deploy/scripts/backup_qdrant.sh`<br>`deploy/k8s/backup.yaml` | Kiểm tra snapshot Qdrant & verify SHA-256 sidecar. |
| **10.4** | `[ ]` | **P1** | **Sổ Tay Xử Lý Sự Cố GPU & Rollback Runbook**: Đủ SOP cho GPU outage (`ModelCircuitBreakerTripped` kèm `agent_model_proxy_fallback_total`), rollback GitOps (`agentic:checkpoint:*` trong Redis) và mục tiêu SLOs. | `docs/runbooks/gpu_failure.md`<br>`docs/runbooks/rollback.md`<br>`docs/slos.md` | Đánh giá tài liệu và diễn tập mô phỏng trên staging. |

---

## 3. Quy Trình Xác Minh & Lệnh Thực Thi (Pre-Flight Runbook)

### Bước 1: Diễn Tập Rollback Bằng ArgoCD / Kubectl
```bash
# Giả lập rollback deployment về revision trước đó
kubectl rollout undo deployment/agentic-api -n agentic-platform

# Xác nhận trạng thái rollout thành công:
kubectl rollout status deployment/agentic-api -n agentic-platform --timeout=60s
```

### Bước 2: Kiểm Tra Trạng Thái Backup, SHA-256 & Restore Drill
```bash
# Smoke drill cục bộ (backup -> SHA-256 -> restore -> verify_audit_chain):
python scripts/restore_drill.py --smoke

# Kiểm tra file snapshot Qdrant trên cluster:
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

> 🎯 **Biên Bản Ký Duyệt**: Kết quả đánh giá của Trụ cột này được tổng hợp và ký duyệt chính thức tại [Bảng Chữ Ký Phê Chuẩn Của Hội Đồng Đánh Giá (Mục 6.3 - REVIEW.md)](REVIEW.md#63-bảng-chữ-ký-phê-chuẩn-của-hội-đồng-đánh-giá).

---

## 6. Bằng Chứng Kiểm Định Tự Động (Auto-Evidence Status)

> **AUTO-EVIDENCE PENDING** — chờ đối soát và **ký bởi Release Board**. Không có nhãn `PASSED` được tự động gán cho tài liệu này.

- Artifact: [evidence/prr/latest.json](../../evidence/prr/latest.json) (`commit_sha`, `timestamp_utc`, exit code của `ruff` / `mypy --no-incremental` / `pytest -n auto --cov=src --cov-fail-under=85` / `eval --mode smoke` / `red_teaming_runner` / `restore_drill --smoke` / parse YAML).
- Sinh artifact: `python scripts/generate_prr_evidence.py`. Tất cả ô trạng thái `[]` ở các mục trên vẫn giữ nguyên cho đến khi Hội đồng đối soát artifact và ký duyệt.
- Bảng chữ ký 9 đại diện: [REVIEW.md mục 6.3](REVIEW.md#63-bảng-chữ-ký-phê-chuẩn-của-hội-đồng-đánh-giá).
