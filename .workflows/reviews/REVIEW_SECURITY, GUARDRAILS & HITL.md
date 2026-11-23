# REVIEW_SECURITY, GUARDRAILS & HITL.md — Trụ Cột 4: An Toàn, Bảo Mật & Phòng Thủ Chiều Sâu

> **Tài liệu kiểm định chuyên biệt (Production Readiness Review - Pillar 04 / 14)**  
> **Đơn vị phụ trách**: SecOps / Security Lead & Cyber Security Architect  
> **Tài liệu tham chiếu**: [REVIEW.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW.md) · [Architecture.md](file:///f:/PROJECTS/AIAgent/Architecture.md) · [AGENTS.md](file:///f:/PROJECTS/AIAgent/AGENTS.md) · [configs/policies.yaml](file:///f:/PROJECTS/AIAgent/configs/policies.yaml)

---

## 1. Mục Tiêu & Phạm Vi Kiểm Định

Trụ cột này thiết lập hệ thống phòng thủ chiều sâu (Defense-in-Depth) nhiều tầng theo nguyên tắc Zero-Trust:
1. **Kiểm Soát Dòng Nhập Liệu (Input Guardrail)**: Phát hiện và ngăn chặn 100% prompt injection, jailbreak, và rò rỉ dữ liệu nhạy cảm (PII Redaction).
2. **Cách Ly Thực Thi Mã (Sandbox Isolation)**: Toàn bộ code execution của CodingAgent phải chạy trong Docker/gVisor sandbox cô lập, giới hạn cứng RAM/CPU và timeout.
3. **Cơ Chế Phê Duyệt Của Con Người (Human-in-the-Loop - HITL)**: Tự động chặn và tạm dừng graph (`interrupt()`) khi phát hiện tác vụ có mức độ rủi ro cao (`risk_level: high`), chờ duyệt qua endpoint `/approvals`.
4. **Bảo Vệ Luồng Xuất SSE (Output Guardrail & PII Hold Buffer)**: Sử dụng buffer trượt kiểm tra PII trước khi stream token về client.
5. **Chính Sách Phân Quyền Công Cụ (Tool Execution Allowlist)**: Khóa chặt danh mục công cụ được phép gọi theo từng Sub-agent.

---

## 2. Danh Mục Checklist Kiểm Tra Chi Tiết

| ID | Trạng Thái | Mức Độ | Tiêu Chí Kiểm Tra (Verification Criteria) | Tệp Mã Nguồn / Cấu Hình | Phương Pháp & Lệnh Kiểm Chứng |
| :---: | :---: | :---: | :--- | :--- | :--- |
| **4.1** | `[ ]` | **P0** | **Ngăn Chặn Prompt Injection & Jailbreak (Input Guardrail)**: Bộ lọc đầu vào phải nhận diện và chặn đứng các mẫu injection phổ biến (system prompt override, roleplay jailbreak, base64 payload). | [src/agentic/safety/guardrails.py](file:///f:/PROJECTS/AIAgent/src/agentic/safety/guardrails.py) | Chạy test suite an toàn đầu vào:<br>`pytest tests/unit/test_guardrails.py -k test_prompt_injection` |
| **4.2** | `[ ]` | **P0** | **Cô Lập Môi Trường Thực Thi Code (Sandbox Execution)**: MCP `code_exec_server` chạy mã Python/Bash hoàn toàn trong container gVisor/Docker không có quyền root (`non-root user`), read-only rootfs, cấm truy cập mạng host. | [src/agentic/tools/servers/code_exec_server.py](file:///f:/PROJECTS/AIAgent/src/agentic/tools/servers/code_exec_server.py) | Chạy unit test kiểm tra sandbox escape:<br>`pytest tests/unit/test_security.py -k test_sandbox_isolation` |
| **4.3** | `[ ]` | **P0** | **Cơ Chế Phê Duyệt Khi Rủi Ro Cao (HITL Interrupt Node)**: Khi step có `risk_level == 'high'`, LangGraph bắt buộc gọi `interrupt()`, lưu trạng thái checkpoint và trả về trạng thái `WAITING_APPROVAL` ra API. | [src/agentic/orchestrator/nodes/hitl.py](file:///f:/PROJECTS/AIAgent/src/agentic/orchestrator/nodes/hitl.py) | Chạy integration test kiểm tra ngắt HITL:<br>`pytest tests/unit/test_production_hardening.py -k test_hitl_interrupt` |
| **4.4** | `[ ]` | **P0** | **Che Giấu Dữ Liệu Nhạy Cảm Trên Luồng SSE (PII Stream Buffer)**: Dùng buffer trượt để phát hiện và che giấu (redact) số CMND/CCCD, email, token API trước khi stream ra client. | [src/agentic/safety/pii.py](file:///f:/PROJECTS/AIAgent/src/agentic/safety/pii.py) | Chạy unit test cắt lọc PII trên streaming:<br>`pytest tests/unit/test_caching_and_security.py -k test_pii_sse_buffer` |
| **4.5** | `[ ]` | **P0** | **Ràng Buộc Nguồn Gốc CORS (Zero-Trust CORS)**: Khai báo danh sách domain nguồn cụ thể (`allowed_origins`). Nghiêm cấm wildcard `*` trên môi trường Production khi bật `allow_credentials: true`. | [src/agentic/api/main.py](file:///f:/PROJECTS/AIAgent/src/agentic/api/main.py)<br>[src/agentic/core/config.py](file:///f:/PROJECTS/AIAgent/src/agentic/core/config.py) | Chạy unit test bảo vệ CORS:<br>`pytest tests/unit/test_caching_and_security.py -k test_cors_origin_strict` |
| **4.6** | `[ ]` | **P1** | **Chính Sách Phân Quyền Công Cụ (MCP Tool Allowlist)**: File cấu hình `policies.yaml` phân bổ rõ ràng: `ChatAgent` không có tool thực thi lệnh; `CodingAgent` chỉ có tool đọc repo & chạy code trong sandbox. | [configs/policies.yaml](file:///f:/PROJECTS/AIAgent/configs/policies.yaml) | Kiểm tra `policies.yaml` và test `test_tool_allowlist_enforcement`. |

---

## 3. Quy Trình Xác Minh & Lệnh Thực Thi (Pre-Flight Runbook)

### Bước 1: Kiểm Thử Toàn Bộ Suite Bảo Mật & Guardrails
```bash
pytest tests/unit/test_guardrails.py tests/unit/test_security.py tests/unit/test_caching_and_security.py -v -k "test_prompt_injection or test_pii or test_cors or test_hitl"
```

### Bước 2: Kiểm Thử Bộ Đệm Cắt Lọc PII
```bash
python -c "
from src.agentic.safety.pii import redact_pii_stream
text = 'Xin chào, số điện thoại của tôi là 0912345678 và CCCD 012345678901'
print('Redacted output:', list(redact_pii_stream([text])))
"
```

### Bước 3: Diễn Tập Luồng Duyệt HITL Qua API
```bash
# 1. Gửi lệnh rủi ro cao để kích hoạt ngắt graph:
curl -s -X POST http://localhost:8000/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Chạy lệnh bash rm -rf /workspace", "thread_id": "hitl_audit_01"}' | jq .

# 2. Duyệt yêu cầu qua /approvals:
curl -s -X POST http://localhost:8000/v1/approvals \
  -H "Content-Type: application/json" \
  -d '{"thread_id": "hitl_audit_01", "decision": "rejected", "reason": "Lệnh nguy hiểm bị từ chối"}' | jq .
```

---

## 4. Danh Mục Giám Sát Sau Triển Khai (Post-Deployment Hypercare)

Sau khi hệ thống vận hành chính thức:
- [ ] **T+1 Giờ**: Giám sát log của Input Guardrail và Tool Guardrail, kiểm tra xem có phát sinh false positive đối với các câu lệnh hợp lệ của người dùng không.
- [ ] **T+24 Giờ**: Xem xét các tác vụ chờ duyệt Human-in-the-Loop trên dashboard quản trị (bảo đảm không có yêu cầu nào bị treo quá SLA 15 phút).
- [ ] **T+24 Giờ**: Rà soát nhật ký phân tích bảo mật để phát hiện sớm các đợt thăm dò tấn công prompt injection hoặc bypass guardrail.
- [ ] **T+7 Ngày**: Kiểm toán định kỳ danh sách quyền gọi công cụ (Tool Allowlist) và xem xét nhật ký các hành động bị từ chối phê duyệt.

---

## 5. Trạng Thái Nghiệm Thu & Liên Kết Ký Duyệt

- **Tổng số tiêu chí kiểm định**: 6 (P0: 5, P1: 1, P2: 0)
- **Đạt**: _____ / 6
- **Kết luận**: [ ] ĐẠT (PASS) &nbsp;&nbsp;&nbsp; [ ] CẦN KHẮC PHỤC (REJECT)

> 🎯 **Biên Bản Ký Duyệt**: Kết quả đánh giá của Trụ cột này được tổng hợp và ký duyệt chính thức tại [Bảng Chữ Ký Phê Chuẩn Của Hội Đồng Đánh Giá (Mục 6.3 - REVIEW.md)](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW.md#63-bảng-chữ-ký-phê-chuẩn-của-hội-đồng-đánh-giá).

---

## 6. Bằng Chứng Kiểm Định Tự Động (Auto-Evidence Status)

> **AUTO-EVIDENCE PENDING** — chờ đối soát và **ký bởi Release Board**. Không có nhãn `PASSED` được tự động gán cho tài liệu này.

- Artifact: [evidence/prr/latest.json](file:///f:/PROJECTS/AIAgent/evidence/prr/latest.json) (`commit_sha`, `timestamp_utc`, exit code của `ruff` / `mypy` / `pytest --collect-only` / `eval --mode smoke` / parse YAML).
- Sinh artifact: `python scripts/generate_prr_evidence.py`. Tất cả ô trạng thái `[]` ở các mục trên vẫn giữ nguyên cho đến khi Hội đồng đối soát artifact và ký duyệt.
- Bảng chữ ký 9 đại diện: [REVIEW.md mục 6.3](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW.md#63-bảng-chữ-ký-phê-chuẩn-của-hội-đồng-đánh-giá).
