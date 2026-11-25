# Source Quality 10/10 Evidence — 2026-11-28

## Phạm vi kết luận

Source code đạt **10/10 theo bộ tiêu chí chất lượng được cưỡng chế trong repository** sau đợt hardening này. Kết luận chỉ bao gồm thiết kế mã, static analysis, automated tests và coverage; không dùng để tuyên bố production readiness hay go-live readiness.

## Thay đổi chất lượng chính

- Mở rộng Ruff từ nhóm cơ bản `E/F/I/W` sang `ASYNC`, `B`, `C4`, `RUF`, `SIM`, `UP`; chỉ giữ ba nhóm ngoại lệ có giải thích cho API timeout, FastAPI dependency descriptors và test mock contexts.
- Nâng coverage gate của pytest và coverage.py từ 85% lên 90%.
- Nâng ngưỡng đo độc lập lên line 92%, branch 81.5%, core line 98% và bổ sung ngưỡng line/branch riêng cho 10 module về identity, middleware, migration, state, tracing, approvals, audit, guardrails và MCP security.
- Sửa A2A Ed25519 envelope để tự sinh fingerprint `kid`, chọn đúng active/previous verification key khi xoay khóa và từ chối `kid` không tin cậy.
- Refactor OpenTelemetry initialization thành hàm idempotent có thể test; strict environment fail-closed nếu bật tracing nhưng thiếu hoặc không khởi tạo được OTLP exporter.
- Chuyển việc đọc JSONL trong async eval runners sang `asyncio.to_thread`, đồng thời kiểm tra từng record phải là JSON object.
- Bổ sung 28 test tập trung cho các nhánh rủi ro cao: A2A tamper/rotation, audit chain/rotation, tracing, migration advisory lock, PostgreSQL checkpointer, thread registry và MCP transport/security.
- Chuẩn hóa thêm exception chaining, UTC datetime, immutable class constants, import/type hygiene và các simplification an toàn trên toàn kho.

## Kết quả gate cuối

Lệnh xác minh bắt buộc:

```text
make lint
make test
uv lock --check
git diff --check
```

Kết quả:

- Ruff: PASS.
- mypy: PASS, 104 source files.
- pytest: **554 passed, 0 failed**.
- Pytest combined coverage: **90.43%** (gate 90%).
- Measured line coverage: **92.78%** (gate 92%).
- Measured branch coverage: **82.62%** (gate 81.5%).
- Core line coverage: **98.08%** (gate 98%).
- `uv lock --check`: PASS, 250 packages resolved.
- `git diff --check`: PASS; chỉ có cảnh báo chuyển đổi LF/CRLF của Git trên Windows, không có whitespace error.

## Coverage các module trọng yếu

| Module | Line | Branch | Gate |
| --- | ---: | ---: | --- |
| `a2a/identity.py` | 100.00% | 100.00% | PASS |
| `api/middleware.py` | 86.93% | 68.85% | PASS |
| `memory/migrations.py` | 100.00% | 100.00% | PASS |
| `memory/short_term.py` | 91.75% | 86.36% | PASS |
| `memory/thread_registry.py` | 99.28% | 91.30% | PASS |
| `observability/tracing.py` | 100.00% | 100.00% | PASS |
| `safety/approvals.py` | 89.52% | 78.95% | PASS |
| `safety/audit.py` | 99.29% | 100.00% | PASS |
| `safety/guardrails.py` | 95.67% | 87.50% | PASS |
| `tools/servers/security.py` | 98.33% | 100.00% | PASS |

## Diễn giải “10/10”

“10/10” ở đây có nghĩa là toàn bộ tiêu chí source-quality hiện được định nghĩa, tăng cường và tự động cưỡng chế trong repository đều đạt. Đây không phải khẳng định phần mềm tuyệt đối không còn lỗi; mọi thay đổi tương lai vẫn phải đi qua cùng các gate để duy trì kết quả này.
