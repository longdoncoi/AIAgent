# AGENTS_SAFETY_HITL.md — Chính Sách An Toàn & Phê Duyệt Con Người (Safety & HITL - L8)

> **Tài liệu tham chiếu chuyên sâu** thuộc phân vùng kiến trúc Multi-Agent trong hệ sinh thái `agentic-platform`.  
> Tài liệu gốc: [AGENTS.md](file:///f:/PROJECTS/AIAgent/AGENTS.md) · [Architecture.md](file:///f:/PROJECTS/AIAgent/Architecture.md).

---

## 1. Cơ Chế Phòng Vệ Đa Tầng (Multi-Layer Guardrails)

Hệ thống triển khai cơ chế phòng vệ nhiều lớp theo quy tắc tại `configs/policies.yaml`:

```
Input Stream ──► [Input Guardrail: PII, Jailbreak, Injection]
                      │ (Hợp lệ)
                      ▼
                 [LangGraph Supervisor]
                      │
                      ├─► [Risk Level: Low/Medium] ──► Chạy tự động
                      │
                      └─► [Risk Level: High]
                               │
                               ▼
                       [HITL Interrupt Node]
                               │ (Tạm dừng, đẩy event ra SSE)
                               ▼
                        [Chờ Duyệt qua /approvals]
                               │
                               ├─► [Approved] ──► Resume graph
                               └─► [Rejected] ──► Abort & phản hồi lý do
```

---

## 2. Phân Cấp Mức Độ Rủi Ro (Risk Level Categorization)

- **Rủi ro mức độ thấp (Low Risk)**:
  - Hành vi: Đọc tài liệu, tìm kiếm web, chat thông thường.
  - Xử lý: Tự động thực thi, ghi log chuẩn.
- **Rủi ro mức độ trung bình (Medium Risk)**:
  - Hành vi: Chạy code thử nghiệm trong sandbox đọc dữ liệu tạm.
  - Xử lý: Tự động thực thi có log cảnh báo và giới hạn thời gian (timeout)/tài nguyên nghiêm ngặt.
- **Rủi ro mức độ cao (High Risk)**:
  - Hành vi: Ghi đè file dự án, gọi API bên thứ ba có phí, xoá dữ liệu, thực thi bash hệ thống.
  - Xử lý: **Bắt buộc kích hoạt HITL (Human-in-the-Loop)**. Trạng thái phiên làm việc dừng lại tại `interrupt()` và chỉ tiếp tục sau khi có tín hiệu phê duyệt hợp lệ qua endpoint `/approvals`.

---

## 3. Capability Phê Duyệt & Ranh Giới Khóa

### 3.1 Luồng xác minh

1. Chỉ Supervisor/API nhận `APPROVAL_PRIVATE_KEY` và ký step capability Ed25519 sau quyết định HITL.
2. L3 Agent nhận `APPROVAL_PUBLIC_KEY`, xác minh owner/step/fingerprint/tool/TTL rồi attenuate capability thành đúng một tool call với digest arguments và nonce riêng. Agent không có vật liệu ký grant gốc.
3. L4 MCP nhận `APPROVAL_PUBLIC_KEY`, xác minh cả chữ ký step lẫn chuỗi attenuation, từ chối capability mở rộng quyền, sai arguments, hết hạn hoặc replay nonce.
4. Production `SERVICE_ROLE` fail startup nếu API thiếu private key, verifier thiếu public key, hoặc private key bị cấp nhầm cho agent/MCP/maintenance.
5. Kubernetes dùng Secret riêng cho API, từng Chat/RAG/Coding agent, MCP, maintenance và database; tuyệt đối không khôi phục shared `agentic-secrets`.

---

## 4. Tài Liệu Kiểm Định & Tham Chiếu Liên Quan

- **Kiểm định an toàn, bảo mật & phòng thủ chiều sâu**: [REVIEW_SECURITY, GUARDRAILS & HITL.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_SECURITY,%20GUARDRAILS%20&%20HITL.md)
- **Kiểm định tuân thủ pháp lý, quyền riêng tư & đạo đức AI**: [REVIEW_COMPLIANCE, PRIVACY & AI ETHICS.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_COMPLIANCE,%20PRIVACY%20&%20AI%20ETHICS.md)
- **Kiểm định quản trị chi phí & giới hạn hạn ngạch**: [REVIEW_FINOPS & COST GOVERNANCE.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_FINOPS%20&%20COST%20GOVERNANCE.md)
