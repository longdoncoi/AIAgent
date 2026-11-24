# Active context

> Cập nhật: 2026-11-24. Đây là snapshot bàn giao; lịch sử chi tiết nằm trong Git.

## Trạng thái hiện tại

- Baseline đang chờ commit trên nền `419019c`; runtime chuẩn là Python 3.12 và dependency graph được khóa bởi `uv.lock`.
- A2A runtime dùng SDK/protocol v1.0: discovery chuẩn, JSON-RPC/SSE, PostgreSQL task store và shared pooled client; compatibility routes không được Supervisor dùng ở production.
- Authorization fail-closed tại A2A và MCP: S2S token cùng capability có chữ ký bị ràng buộc theo run, step, owner, agent và tool.
- Checkpoint production dùng `AsyncPostgresSaver`; Redis từ chối payload không có HMAC hợp lệ; tenant/RAG state được phân vùng theo execution context.
- Deployment đã có Alembic PreSync migration job, sandbox, non-root/read-only filesystem, NetworkPolicy/Istio policy, ExternalSecret và image/toolchain pins.

## Công việc đang mở

1. Commit/push thay đổi hardening, build/pin image mới và xác nhận toàn bộ GitHub Actions/Trivy xanh.
2. Chạy Alembic upgrade và smoke test staging với Postgres, Redis, Qdrant, LiteLLM, A2A và MCP thật.

## Blocker

- Chưa có Kubernetes staging context và managed dependencies để chạy smoke test end-to-end.

## Baseline kiểm chứng gần nhất

- Ruff, Mypy strict, dependency audit, documentation check, Alembic offline upgrade, Kustomize render và full test suite kèm coverage đều pass trên workspace hiện tại.
- Không giữ số lượng test/coverage trong snapshot vì chúng thay đổi theo commit. Dùng CI hoặc chạy `make test` để lấy số hiện tại.

## Ghi chú bàn giao

- Hệ thống tài liệu đang dùng progressive disclosure: bắt đầu từ `AGENTS.md`, chỉ mở memory/skill/workflow liên quan.
- Không nối thêm danh sách “đã hoàn thành” vào tệp này; commit history là changelog.
