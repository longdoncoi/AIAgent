# REVIEW_CI-CD & GITOPS.md — Trụ Cột 9: Quy Trình CI/CD, Chuỗi Cung Ứng & Triển Khai GitOps

> **Tài liệu kiểm định chuyên biệt (Production Readiness Review - Pillar 09 / 14)**  
> **Đơn vị phụ trách**: DevOps Engineer & CI/CD Pipeline Architect  
> **Tài liệu tham chiếu**: [REVIEW.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW.md) · [Architecture.md](file:///f:/PROJECTS/AIAgent/Architecture.md) · [.github/workflows/ci.yml](file:///f:/PROJECTS/AIAgent/.github/workflows/ci.yml) · [deploy/k8s/argocd-application.yaml](file:///f:/PROJECTS/AIAgent/deploy/k8s/argocd-application.yaml)

---

## 1. Mục Tiêu & Phạm Vi Kiểm Định

Trụ cột này bảo đảm quy trình tự động hóa chu kỳ tích hợp và chuyển giao liên tục (CI/CD) chuẩn hóa và an toàn:
1. **Kiểm Tra Mã Nguồn Tĩnh**: Bắt buộc 100% vượt qua `ruff check` và `mypy` không còn cảnh báo.
2. **Quét Lỗ Hổng Bảo Mật Thư Viện**: Quét `pip-audit` đảm bảo không có lỗ hổng CVE Critical/High trong phụ thuộc.
3. **Quét Lỗ Hổng Container Image**: Trivy Scanner quét toàn bộ các images (`api`, `agent`, `mcp`), đảm bảo 0 lỗi Critical chưa vá.
4. **Đóng Gói Bất Biến (Immutable Release Tags)**: Cấm dùng tag `:latest` trên Production; sử dụng Git commit SHA hoặc release tag chuẩn.
5. **GitOps Synchronization với ArgoCD**: ArgoCD quản lý khai báo trạng thái mong muốn (declarative desired state), tự động phát hiện lệch cấu hình (drift detection) và tự phục hồi (self-healing).

---

## 2. Danh Mục Checklist Kiểm Tra Chi Tiết

| ID | Trạng Thái | Mức Độ | Tiêu Chí Kiểm Tra (Verification Criteria) | Tệp Mã Nguồn / Cấu Hình | Phương Pháp & Lệnh Kiểm Chứng |
| :---: | :---: | :---: | :--- | :--- | :--- |
| **9.1** | `[ ]` | **P0** | **Kiểm Tra Mã Nguồn Tĩnh (Linting & Static Type Check)**: Toàn bộ code trong `src/` và `tests/` phải vượt qua `ruff check` và `mypy` không còn bất kỳ cảnh báo hoặc lỗi type hints nào. | [pyproject.toml](file:///f:/PROJECTS/AIAgent/pyproject.toml) | Thực thi linting kiểm tra:<br>`ruff check src/ tests/`<br>`mypy src/` |
| **9.2** | `[ ]` | **P0** | **Quét Lỗ Hổng Bảo Mật Thư Viện (pip-audit Scan)**: Không có lỗ hổng bảo mật nghiêm trọng (CRITICAL / HIGH CVEs) trong danh sách dependencies của Python. | [pyproject.toml](file:///f:/PROJECTS/AIAgent/pyproject.toml) | Chạy kiểm tra bảo mật thư viện:<br>`pip-audit` |
| **9.3** | `[ ]` | **P0** | **Quét Lỗ Hổng Container Image (Trivy Scan)**: Các images (`api`, `agent`, `mcp`) phải vượt qua bước quét Trivy Container Scanner trong CI pipeline với 0 lỗi CRITICAL chưa vá. | [.github/workflows/ci.yml](file:///f:/PROJECTS/AIAgent/.github/workflows/ci.yml) | Kiểm tra kết quả job `build-and-scan` trong GitHub Actions. |
| **9.4** | `[ ]` | **P0** | **Đóng Gói Image Bất Biến (Immutable SHA Tagging)**: Cấm tuyệt đối sử dụng tag `:latest` trên production. Mọi deployment phải trỏ tới immutable digest hoặc commit SHA (ví dụ: `ghcr.io/agentic-platform/api:${{ github.sha }}}`). | [.github/workflows/ci.yml](file:///f:/PROJECTS/AIAgent/.github/workflows/ci.yml)<br>[deploy/k8s/deployment.yaml](file:///f:/PROJECTS/AIAgent/deploy/k8s/deployment.yaml) | Kiểm tra manifest sau khi gitops auto-update. |
| **9.5** | `[ ]` | **P1** | **Tự Động Đồng Bộ GitOps (ArgoCD Synchronization)**: Khai báo ArgoCD Application (`agentic-platform-prod`), tự động phát hiện thay đổi trên branch `main` hoặc release tag `v*.*.*`, hỗ trợ self-healing và auto-prune. | [deploy/k8s/argocd-application.yaml](file:///f:/PROJECTS/AIAgent/deploy/k8s/argocd-application.yaml) | Kiểm tra trạng thái ứng dụng trên giao diện ArgoCD: `Synced` & `Healthy`. |

---

## 3. Quy Trình Xác Minh & Lệnh Thực Thi (Pre-Flight Runbook)

### Bước 1: Thực Thi Linting & Type Checking Toàn Dự Án
```bash
ruff check src/ tests/
mypy src/
```

### Bước 2: Quét Bảo Mật Dependencies Bằng pip-audit
```bash
pip-audit
```

### Bước 3: Build & Quét Thử Nghiệm Container Bằng Trivy
```bash
# Build thử nghiệm image API
docker build -f deploy/docker/Dockerfile.api -t agentic-platform/api:audit-test .

# Quét bằng Trivy
trivy image --severity CRITICAL,HIGH agentic-platform/api:audit-test
```

---

## 4. Danh Mục Giám Sát Sau Triển Khai (Post-Deployment Hypercare)

Sau khi hệ thống vận hành chính thức:
- [ ] **T+15 Phút**: Xác nhận trạng thái ứng dụng trên ArgoCD là `Synced` và `Healthy`, không phát hiện lệch cấu hình (no configuration drift).
- [ ] **T+24 Giờ**: Xác nhận toàn bộ container images đang chạy trên production đều mang immutable commit SHA, không có image nào bị pull lại từ tag `:latest`.
- [ ] **T+7 Ngày**: Rà soát nhật ký audit trail của pipeline CI/CD và GitOps repo, đảm bảo mọi đợt deploy đều có PR và code review được phê duyệt đầy đủ.

---

## 5. Trạng Thái Nghiệm Thu & Liên Kết Ký Duyệt

- **Tổng số tiêu chí kiểm định**: 5 (P0: 4, P1: 1, P2: 0)
- **Đạt**: _____ / 5
- **Kết luận**: [ ] ĐẠT (PASS) &nbsp;&nbsp;&nbsp; [ ] CẦN KHẮC PHỤC (REJECT)

> 🎯 **Biên Bản Ký Duyệt**: Kết quả đánh giá của Trụ cột này được tổng hợp và ký duyệt chính thức tại [Bảng Chữ Ký Phê Chuẩn Của Hội Đồng Đánh Giá (Mục 6.3 - REVIEW.md)](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW.md#63-bảng-chữ-ký-phê-chuẩn-của-hội-đồng-đánh-giá).

---

## 6. Bằng Chứng Kiểm Định Tự Động (Auto-Evidence Status)

> **AUTO-EVIDENCE PENDING** — chờ đối soát và **ký bởi Release Board**. Không có nhãn `PASSED` được tự động gán cho tài liệu này.

- Artifact: [evidence/prr/latest.json](file:///f:/PROJECTS/AIAgent/evidence/prr/latest.json) (`commit_sha`, `timestamp_utc`, exit code của `ruff` / `mypy` / `pytest --collect-only` / `eval --mode smoke` / parse YAML).
- Sinh artifact: `python scripts/generate_prr_evidence.py`. Tất cả ô trạng thái `[]` ở các mục trên vẫn giữ nguyên cho đến khi Hội đồng đối soát artifact và ký duyệt.
- Bảng chữ ký 9 đại diện: [REVIEW.md mục 6.3](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW.md#63-bảng-chữ-ký-phê-chuẩn-của-hội-đồng-đánh-giá).
