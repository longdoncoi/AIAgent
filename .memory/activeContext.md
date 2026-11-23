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
- **Thực hiện khuyến nghị sau review mã nguồn toàn diện**:
  - Triệt tiêu 100% lỗi linter Ruff (`I001`, `F401`) trên toàn bộ kho mã nguồn bao gồm `migrations/`.
  - Giữ vững chất lượng tĩnh: `ruff check .` (0 lỗi), `mypy --strict src` (74/74 source files sạch lỗi).
  - Giữ `pytest-xdist` cho tùy chọn local nhưng chuyển CI/Makefile sang chạy tuần tự vì suite dùng singleton, cache và SQLite stateful không an toàn khi chia worker.
  - Xử lý dứt điểm các cảnh báo coroutine unawaited và rò rỉ kết nối `sqlite3.Connection` trong `LongTermStore` bằng `@contextmanager`.
  - Xác nhận toàn bộ 268/268 test cases đều passed với coverage 96% toàn hệ thống (100% trên orchestration, safety và tools).

## Xác minh

- `ruff check .`: PASS (0 cảnh báo/lỗi trên toàn bộ dự án).
- `mypy src/`: PASS (Success: no issues found in 75 source files).
- Full pytest đã chạy hết 277 test với coverage 95%: 276 pass và 1 lỗi tương thích ngưỡng secret; sau khi sửa, nhóm regression liên quan pass 9/9 và nhóm capability/Qdrant pass 13/13.
- Audit graph dependency production từ `uv.lock` trên Python 3.12: PASS, không có vulnerability đã biết.
- Docker Desktop hoạt động; API, Agent và MCP image đã build thành công từ base/uv pin digest và `uv sync --frozen`.
- `kubectl kustomize deploy/k8s`: PASS; sandbox selector render đúng namespace `agentic-sandbox`.

## Việc tiếp theo

1. Push nhánh hardening, mở PR và chờ toàn bộ GitHub Actions (lint/type/audit/test/eval/container build/Trivy) xanh.
2. Chạy smoke test với Postgres/Redis/Qdrant/LiteLLM/A2A/MCP thật trong cluster staging trước khi ký go-live.

## Blocker

- Chưa có Kubernetes staging context và các dependency managed thật để chạy end-to-end smoke test; GitHub CI/Trivy là gate còn lại trong phạm vi repository.

## Hardening production 2026-11-24

- Reset toàn bộ run-scoped state mỗi lượt chat; capability HMAC ngắn hạn khóa theo run/step/owner/agent/tool và được kiểm tra lại ở A2A lẫn MCP server.
- Execution identity được ký cho cả step thường; metadata owner không còn được tin nếu thiếu capability hợp lệ.
- Tenant RAG được ép từ execution context, metadata CDC được lưu sidecar, shared document phải khai báo rõ, filter được áp trước lexical/vector fusion và KG tenant-unsafe bị loại.
- Production embedding/Qdrant fail-closed; long-term PostgreSQL không còn fallback SQLite và khởi tạo store không block event loop.
- Secret tĩnh đã bị xóa; ExternalSecret là owner duy nhất và cung cấp đầy đủ DB/Redis/Qdrant/LiteLLM/embedding settings; Ingress/PgBouncer/NetworkPolicy/Istio policy đã đồng bộ.
- Docker context loại dữ liệu memory/raw/index; image/toolchain/action pin immutable, dependency install/audit đều dùng lockfile Python 3.12.
