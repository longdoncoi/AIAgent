# Feature lifecycle

1. **Khảo sát**: xác định owner module, contract, caller/callee, test và config. Đọc `activeContext.md` chỉ nếu tính năng nối tiếp công việc đang mở.
2. **Thiết kế**: mô tả input/output, failure modes, security/tenant boundary và compatibility. Thay đổi kiến trúc bền vững cần ADR và specification tương ứng.
3. **Test**: thêm test cho happy path, validation, denial/failure và regression quan trọng. Mock external services ở unit layer.
4. **Implement**: thay đổi nhỏ nhất theo [`coding-workflow.md`](coding-workflow.md); cập nhật deployment/docs trong cùng diff nếu contract đổi.
5. **Verify**: chạy test hẹp, quality gate theo phạm vi, rồi soát diff. Dùng [`pr-preflight.md`](pr-preflight.md) khi chuẩn bị PR.
6. **Handoff**: cập nhật `activeContext.md` chỉ khi còn next step/blocker hoặc baseline bàn giao mới; Git là changelog.
