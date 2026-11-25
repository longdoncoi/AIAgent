# REVIEW_CI-CD & GITOPS.md — Trụ Cột 9: Quy Trình CI/CD, Chuỗi Cung Ứng & Triển Khai GitOps

> **Tài liệu kiểm định chuyên biệt (Production Readiness Review - Pillar 09 / 14)**  
> **Đơn vị phụ trách**: DevOps Engineer & CI/CD Pipeline Architect  
> **Tài liệu tham chiếu**: [REVIEW.md](REVIEW.md) · [Architecture.md](../../Architecture.md) · [.github/workflows/ci.yml](../../.github/workflows/ci.yml) · [deploy/k8s/argocd-application.yaml](../../deploy/k8s/argocd-application.yaml)

---

## 1. Mục Tiêu & Phạm Vi Kiểm Định

Trụ cột này bảo đảm quy trình tự động hóa chu kỳ tích hợp và chuyển giao liên tục (CI/CD) chuẩn hóa và an toàn:
1. **Kiểm Tra Mã Nguồn Tĩnh**: Bắt buộc 100% vượt qua `ruff check` và `mypy` không còn cảnh báo.
2. **Quét Bí Mật & Lỗ Hổng Phụ Thuộc**: Gitleaks quét toàn bộ Git history; `pip-audit` đảm bảo không có lỗ hổng CVE Critical/High trong phụ thuộc.
3. **Quét Lỗ Hổng & SBOM Container**: Trivy quét toàn bộ images (`api`, `agent`, `mcp`) và sinh CycloneDX SBOM, đảm bảo 0 lỗi Critical/High theo policy.
4. **Đóng Gói Bất Biến Có Chứng Thực**: Cấm tag `:latest`; release image dùng commit SHA, BuildKit SBOM/provenance và chữ ký keyless Sigstore/Cosign trên digest.
5. **GitOps Synchronization với ArgoCD**: ArgoCD quản lý trạng thái mong muốn và phát hiện drift; production chỉ đồng bộ thủ công sau attestation và phê duyệt Release Board để không vượt qua cổng quản trị.

---

## 2. Danh Mục Checklist Kiểm Tra Chi Tiết

| ID | Trạng Thái | Mức Độ | Tiêu Chí Kiểm Tra (Verification Criteria) | Tệp Mã Nguồn / Cấu Hình | Phương Pháp & Lệnh Kiểm Chứng |
| :---: | :---: | :---: | :--- | :--- | :--- |
| **9.1** | `[ ]` | **P0** | **Kiểm Tra Mã Nguồn Tĩnh (Linting & Static Type Check)**: Toàn bộ code trong `src/`, `evals/`, `scripts/` và `tests/` phải vượt qua `ruff check`; code vận hành trong `src/`, `evals/`, `scripts/` phải vượt qua `mypy` không còn bất kỳ lỗi type hints nào. | [pyproject.toml](../../pyproject.toml) | Thực thi linting kiểm tra:<br>`ruff check .`<br>`mypy src/ evals/ scripts/` |
| **9.2** | `[ ]` | **P0** | **Quét Bí Mật & Lỗ Hổng Phụ Thuộc**: Gitleaks `8.30.1` đã kiểm checksum quét toàn bộ reachable Git history; exception chỉ dùng fingerprint đã review. `pip-audit` không phát hiện lỗ hổng trong graph production đã khóa. | [.gitleaks.toml](../../.gitleaks.toml)<br>[.gitleaksignore](../../.gitleaksignore)<br>[pyproject.toml](../../pyproject.toml) | Chạy `gitleaks git . --config .gitleaks.toml --gitleaks-ignore-path .gitleaksignore --redact=100` và `pip-audit` theo CI. |
| **9.3** | `[ ]` | **P0** | **Quét Lỗ Hổng & Xuất SBOM Container**: Images `api`, `agent`, `mcp` phải đạt Trivy với 0 lỗi CRITICAL/HIGH theo policy và mỗi image có artifact CycloneDX SBOM lưu 90 ngày. | [.github/workflows/ci.yml](../../.github/workflows/ci.yml) | Kiểm tra job matrix `build-and-scan` và ba artifact `sbom-<component>-<sha>`. |
| **9.4** | `[ ]` | **P0** | **Đóng Gói Image Bất Biến Có Chứng Thực**: Cấm `:latest`; deployment trỏ commit SHA/digest. Release build phải xuất BuildKit SBOM + provenance `mode=max`, ký keyless Cosign trên digest và verify ngay theo OIDC identity của workflow. | [.github/workflows/ci.yml](../../.github/workflows/ci.yml)<br>[deploy/k8s/deployment.yaml](../../deploy/k8s/deployment.yaml) | Kiểm tra manifest sau GitOps update; chạy `cosign verify` với issuer `https://token.actions.githubusercontent.com` và workflow identity đúng repo/tag. |
| **9.5** | `[ ]` | **P1** | **Đồng Bộ GitOps Có Kiểm Soát (ArgoCD Synchronization)**: Khai báo ArgoCD Application (`agentic-platform-prod`) để phát hiện drift; không cấu hình `automated`, `prune` hay `selfHeal`. Production đồng bộ thủ công sau attestation hai commit và phê duyệt Release Board, sau đó bắt buộc đạt `Synced` & `Healthy`. | [deploy/k8s/argocd-application.yaml](../../deploy/k8s/argocd-application.yaml) | Kiểm tra manifest không có `syncPolicy.automated`; sau phê duyệt, kiểm tra ứng dụng trên ArgoCD đạt `Synced` & `Healthy`. |

---

## 3. Quy Trình Xác Minh & Lệnh Thực Thi (Pre-Flight Runbook)

### Bước 1: Thực Thi Linting & Type Checking Toàn Dự Án
```bash
ruff check src/ tests/
mypy src/ evals/ scripts/
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

### Bước 4: Tạo Attestation Không Tự-Tham-Chiếu

1. Chốt source commit `C`, chạy đủ gate và lấy 9 chữ ký cho SHA đầy đủ của `C` cùng release tag dự kiến.
2. Tạo commit `A` kế tiếp chỉ chứa `evidence/signoff/release-board.json`; trường `commit_sha` phải bằng `C`.
3. Tạo release tag trỏ vào `A`. Job phát hành tải `prr-evidence-<SHA-A>` từ workflow hiện tại, kiểm tra `parent(A)=C` và từ chối nếu diff của `A` chứa bất kỳ path nào khác.
4. Không amend `A`, không chèn code/config sau khi ký. Mọi thay đổi làm mất hiệu lực chữ ký và yêu cầu quay lại bước 1.

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

> 🎯 **Biên Bản Ký Duyệt**: Kết quả đánh giá của Trụ cột này được tổng hợp và ký duyệt chính thức tại [Bảng Chữ Ký Phê Chuẩn Của Hội Đồng Đánh Giá (Mục 6.3 - REVIEW.md)](REVIEW.md#63-bảng-chữ-ký-phê-chuẩn-của-hội-đồng-đánh-giá).

---

## 6. Bằng Chứng Kiểm Định Tự Động (Auto-Evidence Status)

> **AUTO-EVIDENCE PENDING** — chờ đối soát và **ký bởi Release Board**. Không có nhãn `PASSED` được tự động gán cho tài liệu này.

- Artifact: [evidence/prr/latest.json](../../evidence/prr/latest.json) (`commit_sha`, `timestamp_utc`, exit code của `ruff` / `mypy --no-incremental` / `pytest -n auto --cov=src --cov-fail-under=85` / `eval --mode smoke` / `red_teaming_runner` / `restore_drill --smoke` / parse YAML).
- Sinh artifact: `python scripts/generate_prr_evidence.py`. Tất cả ô trạng thái `[]` ở các mục trên vẫn giữ nguyên cho đến khi Hội đồng đối soát artifact và ký duyệt.
- Bảng chữ ký 9 đại diện: [REVIEW.md mục 6.3](REVIEW.md#63-bảng-chữ-ký-phê-chuẩn-của-hội-đồng-đánh-giá).
