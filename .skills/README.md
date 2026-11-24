# `.skills` — runbook nạp theo tác vụ

Mỗi skill nằm trong `<name>/SKILL.md`; chỉ mở skill khớp yêu cầu.

| Trigger | Skill |
| --- | --- |
| chạy/chọn test, coverage, mock | [`test-coverage`](test-coverage/SKILL.md) |
| Ruff, Mypy, audit, code-quality gate | [`code-quality`](code-quality/SKILL.md) |
| thêm hoặc sửa L3 A2A sub-agent | [`add-subagent`](add-subagent/SKILL.md) |
| thêm hoặc sửa MCP tool/policy | [`add-mcp-tool`](add-mcp-tool/SKILL.md) |
| đổi PostgreSQL schema/Alembic | [`database-migration`](database-migration/SKILL.md) |
| đánh giá prompt/routing/RAG | [`evaluate-ai-quality`](evaluate-ai-quality/SKILL.md) |

Skill chứa quy trình; chi tiết kiến trúc thuộc `.workflows/specifications/`, còn lệnh thực thi thuộc `Makefile`. Không sao chép cùng một bảng cấu hình vào nhiều skill.
