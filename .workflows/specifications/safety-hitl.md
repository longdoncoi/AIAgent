# AGENTS_SAFETY_HITL.md — Chính Sách An Toàn & Phê Duyệt Con Người (Safety & HITL - L8)

> **Tài liệu tham chiếu chuyên sâu** thuộc phân vùng kiến trúc Multi-Agent trong hệ sinh thái `agentic-platform`.  
> Tài liệu gốc: [AGENTS.md](../../AGENTS.md) · [Architecture.md](../../Architecture.md).

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
                        [Chờ Duyệt qua /api/v1/approvals]
                               │  (khớp tenant_id + four-eyes)
                               ├─► [Approved] ──► Resume graph (kèm args digest)
                               └─► [Rejected] ──► Abort & phản hồi lý do
```

- **AuthN & Edge Boundary (`deps.py`, `middleware.py`, `config.py`, `deploy/k8s/ingress.yaml`)**:
  - Đặt mặc định `require_authentication: bool = True` (fail-closed) và áp dụng toàn bộ bất biến bảo mật thông qua `is_strict_environment` / `is_strict_env` cho **cả `staging` lẫn `production`** (CI release gate cũng khóa đầy đủ secret cho `staging`). Token phát triển/kiểm thử (`admin_token`, `test_token`, `secret`) chỉ hợp lệ khi `environment == "test"`, tuyệt đối từ chối ở `development`/`staging`/`production`.
  - Hỗ trợ xác thực JWT qua OIDC/JWKS (`JWT_JWKS_URL`, ép thuật toán cho phép qua `jwt_allowed_algorithms`) song song secret local/test, kèm kiểm tra thu hồi token theo `jti` trong `redis-state` (`JWT_REVOCATION_CHECK`, fail-closed HTTP `503` khi Redis state gián đoạn ở `staging`/`production`) và cache xác thực JWT theo request off-event-loop (`asyncio.to_thread`).
  - Rate limiter giữ fail-closed mặc định ở `staging`/`production` (`RATE_LIMIT_DEGRADED_LOCAL_FALLBACK="false"` trong K8s ConfigMap).
  - Public Ingress tắt CORS ở lớp NGINX (`nginx.ingress.kubernetes.io/enable-cors: "false"`, FastAPI giữ nguồn CORS chuẩn duy nhất) và chỉ mở `/api/v1/chat`, `/api/v1/approvals`, `/healthz`; `/api/v1/admin/*` chỉ truy cập nội bộ qua `allow-agentic-api` NetworkPolicy.
- **Guardrails chính xác ngữ cảnh (`guardrails.py`, `redaction.py`)**:
  - Chặn lệnh nguy hiểm theo cụm từ cụ thể (`shutdown -h`, `shutdown now`, `shutdown /s`), kiểm tra obfuscated base64 trên cả bản gốc lẫn bản đã làm sạch zero-width/homoglyph, mở rộng `TOXICITY_PATTERNS` (tự hại, chất nổ, đe dọa bạo lực, kích động thù ghét, quấy rối nghiêm trọng).
  - Thứ tự `mask_pii` che giấu API key/secret (`sk-*`, `ghp_*`) trước số điện thoại để tránh cắt nhầm khóa xác thực; nhận diện số điện thoại Việt Nam có khoảng trắng (`0912 345 678`, `+84 ...`), kiểm tra Luhn + ngữ cảnh cho thẻ tín dụng, phân biệt 9 chữ số CMND theo ngữ cảnh so với 12 chữ số CCCD, bảo toàn khối `[CITATIONS]` khi cache, không chặn nhầm cụm học thuật như `"hate speech"`, và bọc lỗi `sanitize_context` an toàn trong luồng A2A `execute_stream`.

---

## 2. Phân Cấp Mức Độ Rủi Ro (Risk Level Categorization)

- **Rủi ro mức độ thấp (Low Risk)**:
  - Hành vi: Đọc tài liệu, tìm kiếm web, chat thông thường.
  - Xử lý: Tự động thực thi, ghi log chuẩn.
- **Rủi ro mức độ trung bình (Medium Risk)**:
  - Hành vi: Chạy code thử nghiệm trong sandbox đọc dữ liệu tạm.
  - Xử lý: Tự động thực thi có log cảnh báo và giới hạn thời gian (timeout)/tài nguyên nghiêm ngặt.
- **Rủi ro mức độ cao (High Risk)**:
  - Hành vi: Ghi đè file dự án (`repo_write_file`), thực thi Python sandbox (`code_exec_python`), thực thi bash hệ thống (`code_exec_bash`), gọi API bên thứ ba có phí, xoá dữ liệu, **hoặc mọi tool chưa khai báo trong `configs/policies.yaml` (default-deny)**.
  - Xử lý: Phân loại rủi ro gắn với từng tool và năng lực đột biến của agent (`_apply_risk_policy` không leo thang nhầm các câu chat/RAG như *"How do I create a ticket?"*). Kích hoạt **HITL 2 pha ở mức tool-call** (`REQUIRE_TOOL_CALL_APPROVAL=true`): khi agent định gọi bất kỳ tool nào đánh dấu `policy.is_approval_required(tool_name)` (`code_exec_bash`, `code_exec_python`, `repo_write_file`) mà chưa được duyệt digest đối số chính xác, `ToolNode` trong `build_react_graph` giữ nguyên ngoại lệ `ToolCallApprovalRequired` (không nuốt thành `ToolMessage`) để graph dừng tại `interrupt()` với payload ghi rõ `tool_name` và `tool_arguments` cụ thể. Chỉ tiếp tục sau khi người dùng khác trong cùng `tenant_id` (four-eyes ở `staging`/`production`) phê duyệt qua `/api/v1/approvals`.

---

## 3. Capability Phê Duyệt & Ranh Giới Khóa

### 3.1 Luồng xác minh

1. Chỉ Supervisor/API nhận `APPROVAL_PRIVATE_KEY` và ký step capability Ed25519 (gắn `kid` theo `approval_key_id` và `approved_args_digests` khi duyệt theo tool-call) sau quyết định HITL. Hỗ trợ xoay vòng khóa mượt mà thông qua `approval_previous_public_keys` ánh xạ `kid` cũ.
2. L3 Agent nhận `APPROVAL_PUBLIC_KEY`, xác minh owner/step/fingerprint/tool/args digest/TTL rồi attenuate capability thành đúng một tool call với digest arguments và nonce riêng (`redis-state`). Agent không có vật liệu ký grant gốc.
3. L4 MCP nhận `APPROVAL_PUBLIC_KEY`, xác minh cả chữ ký step lẫn chuỗi attenuation, từ chối capability mở rộng quyền, sai arguments, hết hạn hoặc replay nonce. `repo_server` chặn đọc `data/index`, `data/raw` và file `.env`/`.env.*`/`.envrc` ở mọi độ sâu (trừ `.env.example`), chia sẻ PVC RWX `agentic-mcp-scratch` cùng `code_exec_server` (`stdout/stderr` cắt ở 32 KiB).
4. Namespace `agentic-sandbox` áp đặt Pod Security Standard `restricted`, `ResourceQuota`, `LimitRange` và `NetworkPolicy` chặn egress nội bộ; Istio áp dụng `PeerAuthentication` thuần `STRICT` toàn namespace (không có ngoại lệ `PERMISSIVE`) và `allow-agents-to-mcp-tools` khóa chính xác principal/cổng từng sub-agent (loại trừ `rag`).
5. Production `SERVICE_ROLE` fail startup nếu API thiếu private key, verifier thiếu public key, hoặc private key bị cấp nhầm cho agent/MCP/maintenance.
6. Kubernetes dùng Secret riêng cho API, từng Chat/RAG/Coding agent, MCP, maintenance và database; tuyệt đối không khôi phục shared `agentic-secrets`.

---

## 4. Tài Liệu Kiểm Định & Tham Chiếu Liên Quan

- **Kiểm định an toàn, bảo mật & phòng thủ chiều sâu**: [REVIEW_SECURITY, GUARDRAILS & HITL.md](../reviews/REVIEW_SECURITY,%20GUARDRAILS%20&%20HITL.md)
- **Kiểm định tuân thủ pháp lý, quyền riêng tư & đạo đức AI**: [REVIEW_COMPLIANCE, PRIVACY & AI ETHICS.md](../reviews/REVIEW_COMPLIANCE,%20PRIVACY%20&%20AI%20ETHICS.md)
- **Kiểm định quản trị chi phí & giới hạn hạn ngạch**: [REVIEW_FINOPS & COST GOVERNANCE.md](../reviews/REVIEW_FINOPS%20&%20COST%20GOVERNANCE.md)
