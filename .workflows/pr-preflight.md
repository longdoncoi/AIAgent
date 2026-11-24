# PR pre-flight

Đánh dấu `N/A` có lý do thay vì chạy gate không liên quan.

- [ ] Scope/diff đúng yêu cầu; không ghi đè thay đổi người dùng hoặc chứa file tạm/secret.
- [ ] Test hành vi thay đổi và regression liên quan đã pass.
- [ ] `make lint` pass cho thay đổi Python.
- [ ] `make test` pass khi blast radius yêu cầu full suite.
- [ ] `make audit` chạy khi dependency/build/release thay đổi.
- [ ] Eval phù hợp pass khi prompt, model, routing hoặc RAG thay đổi.
- [ ] Migration dùng expand/contract và đã kiểm upgrade khi schema đổi.
- [ ] A2A/MCP/config/deploy/docs đồng bộ khi contract hoặc topology đổi.
- [ ] Security review bao phủ auth, tenant, capability, HITL và secret boundary.
- [ ] `make docs-check` pass khi Markdown/skill thay đổi.
- [ ] Handoff nêu lệnh/kết quả, gate chưa chạy, risk còn lại và blocker.

`make pre-flight` chạy bộ gate repo mặc định; Production Readiness Review đầy đủ chỉ dùng khi release yêu cầu, bắt đầu từ [`reviews/REVIEW.md`](reviews/REVIEW.md).
