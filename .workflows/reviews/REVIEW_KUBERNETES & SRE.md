# REVIEW_KUBERNETES & SRE.md — Trụ Cột 7: Hạ Tầng Kubernetes, Điều Phối & Tính Sẵn Sàng Cao

> **Tài liệu kiểm định chuyên biệt (Production Readiness Review - Pillar 07 / 14)**  
> **Đơn vị phụ trách**: Principal SRE & Cloud Infrastructure Architect  
> **Tài liệu tham chiếu**: [REVIEW.md](../../.workflows/reviews/REVIEW.md) · [ARCHITECTURE.md](../../Architecture.md) · [AGENTS.md](../../AGENTS.md) · [deploy/k8s/](../../deploy/k8s/)

---

## 1. Mục Tiêu & Phạm Vi Kiểm Định

Trụ cột này bảo đảm hạ tầng triển khai trên Kubernetes được thiết kế theo chuẩn microservices tối tân:
1. **Microservices Tách Biệt Hoàn Toàn (Decoupled Deployments)**: Độc lập giữa API Supervisor, 3 Sub-Agents và 3 MCP servers.
2. **Cấu hình Probes (Liveness & Readiness)**: Khai báo `/healthz` và `/readyz` chính xác, ngăn chặn traffic vào pod chưa sẵn sàng.
3. **Định mức Tài Nguyên (Requests & Limits)**: Cấu hình CPU/RAM requests/limits 100% các containers để chống OOMKilled.
4. **Cô lập Mạng & Bảo Mật Dịch Vụ**: NetworkPolicy phân định luồng giao tiếp; Istio mTLS `STRICT` bắt buộc mã hóa mọi kết nối.
5. **Quản trị Bí Mật Doanh Nghiệp**: Tích hợp ExternalSecrets đồng bộ an toàn từ Vault / Secrets Manager.
6. **Tự Động Mở Rộng (HPA)**: Kích hoạt autoscaling tự động theo tải CPU/RAM.
7. **Durable Orchestration với Temporal**: Temporal cluster và background workers chạy tin cậy.

---

## 2. Danh Mục Checklist Kiểm Tra Chi Tiết

| ID | Trạng Thái | Mức Độ | Tiêu Chí Kiểm Tra (Verification Criteria) | Tệp Mã Nguồn / Cấu Hình | Phương Pháp & Lệnh Kiểm Chứng |
| :---: | :---: | :---: | :--- | :--- | :--- |
| **7.1** | `[ ]` | **P0** | **Kiến Trúc Microservices Tách Rời (Decoupled Deployments)**: Triển khai độc lập: 1 API Supervisor Deployment, 3 Sub-Agent Deployments (`chat`, `rag`, `coding`), 3 MCP Tool Deployments (`code-exec`, `repo`, `web-search`). | [deploy/k8s/deployment.yaml](../../deploy/k8s/deployment.yaml) | Kiểm tra cú pháp và dry-run K8s manifests:<br>`kubectl apply -k deploy/k8s/ --dry-run=client` |
| **7.2** | `[ ]` | **P0** | **Cấu hình Probes (Liveness & Readiness)**: Tất cả các Pods phải khai báo đầy đủ Liveness (`/healthz`) và Readiness (`/readyz`) Probes với initialDelaySeconds và periodSeconds hợp lý, ngăn chặn traffic vào pod chưa sẵn sàng. | [deploy/k8s/deployment.yaml](../../deploy/k8s/deployment.yaml) | Xác thực các endpoints sức khỏe:<br>`curl -f http://localhost:8000/healthz`<br>`curl -f http://localhost:8000/readyz` |
| **7.3** | `[ ]` | **P0** | **Định Mức Tài Nguyên & Chống OOM (Requests & Limits)**: Mọi container phải cấu hình rõ ràng `resources.requests` và `resources.limits` cho cả CPU và RAM, loại bỏ rủi ro tranh chấp hoặc OOMKilled trên node. | [deploy/k8s/deployment.yaml](../../deploy/k8s/deployment.yaml) | Kiểm tra manifests đảm bảo không có container nào thiếu `limits` hoặc `requests`. |
| **7.4** | `[ ]` | **P0** | **Cô Lập Mạng & mTLS (NetworkPolicy & Istio Security)**: `NetworkPolicy` chỉ cho phép API Supervisor gọi Sub-agents, Sub-agents gọi MCP servers. Istio `PeerAuthentication` thiết lập chế độ `STRICT` (mTLS bắt buộc). | [deploy/k8s/network-policy.yaml](../../deploy/k8s/network-policy.yaml)<br>[deploy/k8s/istio-security.yaml](../../deploy/k8s/istio-security.yaml) | Thẩm định các chính sách AuthorizationPolicy và NetworkPolicy bằng `istioctl analyze` hoặc review manifest. |
| **7.5** | `[ ]` | **P0** | **Quản Trị Bí Mật Sản Xuất (ExternalSecrets & Vault)**: Không lưu trữ secret dạng plain-text trong Git hay ConfigMap. Sử dụng `ExternalSecret` đồng bộ tự động từ HashiCorp Vault hoặc AWS Secrets Manager. | [deploy/k8s/external-secret.yaml](../../deploy/k8s/external-secret.yaml) | Kiểm tra CRD ExternalSecret trỏ đúng SecretStore production. |
| **7.6** | `[ ]` | **P1** | **Tự Động Mở Rộng Pod (Horizontal Pod Autoscaler - HPA)**: HPA được kích hoạt cho API và các Sub-Agents (Target CPU: 75%, Target Memory: 80%, Min: 2, Max: 10 replicas). | [deploy/k8s/deployment.yaml](../../deploy/k8s/deployment.yaml) | Kiểm tra định nghĩa HPA trong deployment manifest. |
| **7.7** | `[ ]` | **P1** | **Điều Phối Tác Vụ Bền Vững (Temporal Orchestration)**: Temporal Server (`7233`) hoạt động ổn định; Temporal Worker (`worker.py`) sẵn sàng nhận job CDC và long-running workflow. | [src/agentic/workflows/worker.py](../../src/agentic/workflows/worker.py) | Kiểm tra health của Temporal cluster và worker connection. |

---

## 3. Quy Trình Xác Minh & Lệnh Thực Thi (Pre-Flight Runbook)

### Bước 1: Thẩm Định Manifests Bằng Kustomize & Kubectl
```bash
kubectl apply -k deploy/k8s/ --dry-run=client
```

### Bước 2: Kiểm Tra Tương Thích PgBouncer & Kết Nối CSDL
```bash
pytest tests/unit/test_ten_out_of_ten_hardening.py -v -k "test_pgbouncer"
```

### Bước 3: Kiểm Tra Temporal Durable Execution
```bash
pytest tests/integration/test_chaos_resilience.py -v -k "test_temporal"
```

---

## 4. Danh Mục Giám Sát Sau Triển Khai (Post-Deployment Hypercare)

Sau khi triển khai lên cụm Kubernetes Production:
- [ ] **T+15 Phút**: Xác nhận 100% Pods ở trạng thái `Running` (`kubectl get pods -l tier=l1-interface -n default`).
- [ ] **T+15 Phút**: Xác nhận không có hiện tượng `CrashLoopBackOff`, `OOMKilled` hoặc khởi động lại bất thường.
- [ ] **T+15 Phút**: Ingress và chứng chỉ TLS Certificate hoạt động bình thường, mã phản hồi HTTP `200` từ `/healthz`.
- [ ] **T+24 Giờ**: Đánh giá độ ổn định của Pods sau chu kỳ tự động điều chỉnh HPA (Autoscaling scale-up và scale-down).

---

## 5. Trạng Thái Nghiệm Thu & Liên Kết Ký Duyệt

- **Tổng số tiêu chí kiểm định**: 7 (P0: 5, P1: 2, P2: 0)
- **Đạt**: _____ / 7
- **Kết luận**: [ ] ĐẠT (PASS) &nbsp;&nbsp;&nbsp; [ ] CẦN KHẮC PHỤC (REJECT)

> 🎯 **Biên Bản Ký Duyệt**: Kết quả đánh giá của Trụ cột này được tổng hợp và ký duyệt chính thức tại [Bảng Chữ Ký Phê Chuẩn Của Hội Đồng Đánh Giá (Mục 6.3 - REVIEW.md)](../../.workflows/reviews/REVIEW.md#63-bảng-chữ-ký-phê-chuẩn-của-hội-đồng-đánh-giá).
