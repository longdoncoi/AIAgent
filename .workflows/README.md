# `.workflows` — bộ định tuyến quy trình

Chỉ đọc workflow khớp loại thay đổi; skill cung cấp thao tác chuyên biệt.

| Nhu cầu | Tài liệu |
| --- | --- |
| Quy tắc triển khai chung | [`coding-workflow.md`](coding-workflow.md) |
| Tính năng/hành vi mới | [`feature-lifecycle.md`](feature-lifecycle.md) |
| Điều tra và sửa bug | [`bugfix-workflow.md`](bugfix-workflow.md) |
| Chuẩn bị PR/release | [`pr-preflight.md`](pr-preflight.md) |
| Kiến trúc theo domain | [`specifications/`](specifications/) |
| Production Readiness Review | [`reviews/REVIEW.md`](reviews/REVIEW.md) rồi đúng một checklist liên quan |

## Specification routing

- Agent, graph, A2A endpoint: [`specifications/agent-specifications.md`](specifications/agent-specifications.md)
- A2A/MCP/network/cache: [`specifications/protocols-networking.md`](specifications/protocols-networking.md)
- Checkpoint/long-term memory: [`specifications/state-memory.md`](specifications/state-memory.md)
- Guardrail/capability/HITL: [`specifications/safety-hitl.md`](specifications/safety-hitl.md)
- Trace/metric/eval: [`specifications/observability-evals.md`](specifications/observability-evals.md)
- Bản đồ phát triển: [`specifications/developer-guidelines.md`](specifications/developer-guidelines.md)

Không nạp cả `specifications/` hoặc `reviews/` để xử lý một thay đổi cục bộ.
