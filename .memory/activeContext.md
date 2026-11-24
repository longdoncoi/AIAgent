# Active context

> Cập nhật: 2026-11-24. Đây là snapshot bàn giao; lịch sử chi tiết nằm trong Git.

## Trạng thái hiện tại

- Baseline production-hardening nằm ở commit `419019c` trên nhánh hiện tại; runtime chuẩn là Python 3.12 và dependency graph được khóa bởi `uv.lock`.
- Authorization đang fail-closed tại A2A và MCP: execution identity/capability bị ràng buộc theo run, step, owner, agent và tool.
- Checkpoint production dùng `AsyncPostgresSaver`; Redis từ chối payload không có HMAC hợp lệ; tenant/RAG state được phân vùng theo execution context.
- Deployment đã có sandbox, non-root/read-only filesystem, NetworkPolicy/Istio policy, ExternalSecret và image/toolchain pins.

## Công việc đang mở

1. Đẩy nhánh hardening và xác nhận toàn bộ GitHub Actions/Trivy xanh.
2. Chạy smoke test staging với Postgres, Redis, Qdrant, LiteLLM, A2A và MCP thật.

## Blocker

- Chưa có Kubernetes staging context và managed dependencies để chạy smoke test end-to-end.

## Baseline kiểm chứng gần nhất

- Theo lần kiểm chứng trước thay đổi tài liệu này: Ruff và Mypy pass; Docker images build; `kubectl kustomize deploy/k8s` render thành công.
- Không giữ số lượng test/coverage trong snapshot vì chúng thay đổi theo commit. Dùng CI hoặc chạy `make test` để lấy số hiện tại.

## Ghi chú bàn giao

- Hệ thống tài liệu đang dùng progressive disclosure: bắt đầu từ `AGENTS.md`, chỉ mở memory/skill/workflow liên quan.
- Không nối thêm danh sách “đã hoàn thành” vào tệp này; commit history là changelog.
