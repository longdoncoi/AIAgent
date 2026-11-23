# activeContext.md — Trạng thái hoạt động hiện tại

## Trọng tâm

- Hoàn thiện đợt hardening toàn diện sau source-code review: persistence async, tenant isolation, HITL capability, MCP/A2A fail-closed, sandbox, RAG/CDC, Kubernetes và CI/CD.
- Baseline runtime đã thống nhất ở Python 3.12; `uv.lock` đã được tái tạo.

## Đã hoàn thành

- Chuyển production checkpoint sang `AsyncPostgresSaver` với lifecycle async; Redis checkpoint từ chối payload pickle không ký.
- Scope checkpoint theo chủ sở hữu và bổ sung thread ownership registry phân tán; approval xác thực thread/step đang interrupt.
- Approval được cấp theo từng step và truyền qua A2A bằng execution context; tool rủi ro cao kiểm tra capability tại biên MCP.
- A2A/MCP production fail-closed; MCP dùng transport SSE thật và S2S authentication; local fallback chỉ dành cho dev/test.
- Bổ sung DAG validation, deadlock defense, lọc SSE chỉ ở node `finalize`, và sửa vòng replan dựa trên reflection mới nhất.
- Siết Docker/Kubernetes sandbox, timeout, non-root, read-only FS, capability drop và network deny.
- CDC chống path traversal, thực hiện upsert/delete + rebuild index; RAG cache/retrieval phân vùng theo filter/tenant.
- Readiness, rate limiting, long-term consolidation và cache async path không còn block event loop trực tiếp.
- Đồng bộ Python 3.12, Docker Compose, Kubernetes MCP services/service accounts/network policies và CI security gates.
- Bổ sung regression tests cho DAG, unsigned checkpoint, thread ownership và step-scoped HITL.

## Xác minh

- `ruff check src tests`: pass.
- Regression/security tests: 4/4 pass.
- Full suite trên runtime `uv` Python 3.13: 266/267 pass trong một lượt; test SSE còn lại đã sửa và pass riêng, tương đương 267/267 case được xác minh.
- `mypy --strict`, Ruff, `uv lock --check`, YAML parse và `git diff --check`: pass.
- Docker Desktop 4.100.0 (Docker Engine CLI 29.9.3, Compose 5.7.1) và kubectl 1.37.2 đã được cài đặt; `docker compose config --quiet` và `kubectl kustomize deploy/k8s` đều pass.
- Khắc phục CI run `45181168470`: `pip-audit` nay kiểm tra dependency graph production được export trực tiếp từ `uv.lock`; dependency đánh giá RAGAS chỉ dùng trong dev không còn làm hỏng production audit. Local production audit: không phát hiện lỗ hổng.
- Khắc phục lỗi kế tiếp ở CI run `45183416591`: cả ba runtime image áp dụng Debian security updates trong build để nhận bản vá `zlib1g` cho `CVE-2026-85091`; lint/audit/test của run này đã pass trước khi Trivy phát hiện CVE hệ điều hành.

## Việc tiếp theo

1. Khởi động lại Windows để hoàn tất WSL 2, sau đó khởi động Docker Desktop và thực hiện pre-flight với Docker/Kubernetes dependencies thật trước go-live.
2. Chạy smoke test với Postgres/Redis/LiteLLM/A2A/MCP thực trước khi ký go-live.

## Blocker

- Windows đang báo pending reboot sau khi bật WSL; Docker daemon chưa thể khởi động cho đến khi máy được restart. Kubernetes cluster/context cũng chưa được cấu hình, nên chưa thể smoke-test workload thật.
