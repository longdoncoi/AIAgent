# Vòng thao tác cho coding agent

## 1. Khảo sát

1. Đọc yêu cầu và `AGENTS.md` gần nhất trong phạm vi file sẽ sửa.
2. Chạy `git status --short`; bảo toàn thay đổi sẵn có.
3. Dùng `rg`/`rg --files` để tìm implementation, test và cấu hình chịu trách nhiệm.
4. Chỉ đọc memory khi nhiệm vụ là tiếp nối, bàn giao, release hoặc cần quyết định lịch sử.

## 2. Chọn đúng runbook

| Tác vụ | Tài liệu |
| --- | --- |
| Tính năng | [`../.workflows/feature-lifecycle.md`](../.workflows/feature-lifecycle.md) |
| Sửa lỗi | [`../.workflows/bugfix-workflow.md`](../.workflows/bugfix-workflow.md) |
| PR/release gate | [`../.workflows/pr-preflight.md`](../.workflows/pr-preflight.md) |
| Test/coverage | [`../.skills/test-coverage/SKILL.md`](../.skills/test-coverage/SKILL.md) |
| Lint/type/audit | [`../.skills/code-quality/SKILL.md`](../.skills/code-quality/SKILL.md) |
| Thêm sub-agent | [`../.skills/add-subagent/SKILL.md`](../.skills/add-subagent/SKILL.md) |
| Thêm MCP tool | [`../.skills/add-mcp-tool/SKILL.md`](../.skills/add-mcp-tool/SKILL.md) |
| Migration | [`../.skills/database-migration/SKILL.md`](../.skills/database-migration/SKILL.md) |
| Eval AI/RAG | [`../.skills/evaluate-ai-quality/SKILL.md`](../.skills/evaluate-ai-quality/SKILL.md) |

Không cần đọc workflow lẫn skill không liên quan.

## 3. Thực hiện và xác minh

- Thiết kế thay đổi nhỏ nhất đáp ứng contract hiện có.
- Thêm/sửa test khi hành vi thay đổi; ưu tiên test tái hiện trước với bug.
- Chạy kiểm tra hẹp trong lúc lặp, rồi gate rộng hơn theo rủi ro hoặc theo yêu cầu.
- Soát diff cuối cùng để phát hiện file ngoài phạm vi, secret, generated artifact và tài liệu lệch code.

## 4. Ghi nhớ có chọn lọc

- `activeContext.md`: chỉ trạng thái còn hiệu lực hoặc việc cần bàn giao.
- `architectureDecisions.md`: quyết định kiến trúc bền vững có đánh đổi.
- `systemPatterns.md`: pattern đã được code áp dụng lặp lại.
- `troubleshooting.md`: lỗi đã tái hiện và cách khắc phục đã kiểm chứng.

Không ghi tiến trình theo phút, danh sách file đã đọc, kết quả tạm thời hay nội dung đã tồn tại trong Git.
