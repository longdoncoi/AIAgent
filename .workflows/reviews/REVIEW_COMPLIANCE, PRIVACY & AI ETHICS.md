# REVIEW_COMPLIANCE, PRIVACY & AI ETHICS.md — Trụ Cột 12: Tuân Thủ Pháp Lý, Quyền Riêng Tư & Đạo Đức Trí Tuệ Nhân Tạo

> **Tài liệu kiểm định chuyên biệt (Production Readiness Review - Pillar 12 / 14)**  
> **Đơn vị phụ trách**: Legal Counsel & Data Protection Officer (DPO) & Head of AI Governance  
> **Tài liệu tham chiếu**: [REVIEW.md](../../.workflows/reviews/REVIEW.md) · [ARCHITECTURE.md](../../Architecture.md) · [AGENTS.md](../../AGENTS.md) · [src/agentic/safety/guardrails.py](../../src/agentic/safety/guardrails.py)

---

## 1. Mục Tiêu & Phạm Vi Kiểm Định

Trụ cột này bảo đảm hệ thống `agentic-platform` tuân thủ đầy đủ khung pháp lý hiện hành về an toàn dữ liệu và các tiêu chuẩn đạo đức AI:
1. **Tuân thủ Nghị định 13/2023/NĐ-CP & GDPR**: Thực hiện quyền của chủ thể dữ liệu, đặc biệt là quyền yêu cầu xóa dữ liệu cá nhân (**Right to be Forgotten**) trên toàn bộ các kho lưu trữ (PostgreSQL checkpoints, Redis state, LTM).
2. **Chính Sách Không Lưu Trữ Dữ Liệu Huấn Luyện (Zero Data Retention - ZDR)**: Cam kết 100% dữ liệu hội thoại và tài liệu nội bộ không bị sử dụng để tái huấn luyện mô hình (No Training on Customer Data). Ưu tiên mô hình self-hosted (vLLM, llama.cpp) hoàn toàn trong mạng nội bộ.
3. **Nhật Ký Kiểm Toán & Khả Năng Giải Trình (Audit Trail & Explainability)**: Mọi kế hoạch suy luận (Plan DAG), quyết định gọi công cụ, và lịch sử phê duyệt của con người (HITL) phải được lưu vết có cấu trúc tối thiểu **1 đến 2 năm**, phục vụ thanh tra pháp lý.
4. **Bản Quyền Tri Thức & Bắt Buộc Trích Dẫn (Citations & Provenance)**: Câu trả lời dựa trên tài liệu RAG phải trích dẫn rõ nguồn gốc (tên file, đoạn văn, tác giả), không vi phạm bản quyền sở hữu trí tuệ.
5. **Minh Bạch AI & Tuyên Bố Miễn Trừ Trách Nhiệm (AI Disclaimer)**: Giao diện người dùng bắt buộc hiển thị cảnh báo rõ ràng rằng phản hồi được tạo bởi AI và không thay thế tư vấn chuyên môn pháp lý, y tế hoặc tài chính.
6. **Kiểm Soát Thiên Vị & Ngôn Từ Độc Hại (Fairness & Toxicity Filter)**: Ngăn chặn phản hồi mang tính phân biệt đối xử, quấy rối hoặc kích động bạo lực.

---

## 2. Danh Mục Checklist Kiểm Tra Chi Tiết

| ID | Trạng Thái | Mức Độ | Tiêu Chí Kiểm Tra (Verification Criteria) | Tệp Mã Nguồn / Cấu Hình | Phương Pháp & Lệnh Kiểm Chứng |
| :---: | :---: | :---: | :--- | :--- | :--- |
| **12.1** | `[ ]` | **P0** | **Cơ Chế Xóa Dữ Liệu Theo Yêu Cầu (Right to be Forgotten)**: Có endpoint API hoặc script quản trị cho phép xóa sạch hoàn toàn mọi dữ liệu liên quan đến một `user_id` hoặc `thread_id` trên cả 3 tầng lưu trữ: PostgreSQL checkpoints, Redis session keys, và Long-term Memory. | [src/agentic/memory/short_term.py](../../src/agentic/memory/short_term.py)<br>[src/agentic/memory/long_term.py](../../src/agentic/memory/long_term.py) | Kiểm tra test xóa dữ liệu người dùng:<br>`pytest tests/unit/test_production_hardening.py -k test_delete_user_data` |
| **12.2** | `[ ]` | **P0** | **Chính Sách Zero Data Retention (ZDR)**: Hệ thống sử dụng 100% model nội bộ (vLLM, llama.cpp). Đối với bất kỳ API gateway ngoài nào (nếu dùng fallback), phải có văn bản xác nhận thỏa thuận Doanh nghiệp (Enterprise Agreement) cam kết không lưu dữ liệu để train lại model. | [configs/litellm_config.yaml](../../configs/litellm_config.yaml) | Kiểm tra cấu hình LiteLLM và hợp đồng dịch vụ đám mây (Cloud Service Agreement). |
| **12.3** | `[ ]` | **P0** | **Nhật Ký Kiểm Toán Không Thể Sửa Đổi (Immutable Audit Trail)**: Mọi thao tác nhạy cảm, lệnh bash, quyết định phê duyệt HITL và input/output được ghi vào audit log bảo mật với `trace_id`, lưu trữ tối thiểu 365 ngày trên cold storage. | [src/agentic/orchestrator/nodes/hitl.py](../../src/agentic/orchestrator/nodes/hitl.py) | Xác minh trường nhật ký phê duyệt lưu vào CSDL Postgres kèm timestamp chuẩn UTC. |
| **12.4** | `[ ]` | **P0** | **Bản Quyền Tri Thức & Bắt Buộc Trích Dẫn (Citations)**: RAGAgent bắt buộc kèm danh sách nguồn tham khảo (`citations: [doc_id, page, source]`). Phản hồi không có căn cứ hoặc vi phạm bản quyền bị chặn bởi Reflection node hoặc Output Guard. | [src/agentic/orchestrator/nodes/reflector.py](../../src/agentic/orchestrator/nodes/reflector.py)<br>[src/agentic/rag/tools.py](../../src/agentic/rag/tools.py) | Gửi truy vấn RAG và xác nhận response trả về danh mục `sources` / `citations` đầy đủ. |
| **12.5** | `[ ]` | **P1** | **Minh Bạch AI & Tuyên Bố Miễn Trừ Trách Nhiệm (Disclaimer)**: Giao diện Web UI và API schema chứa trường disclaimer pháp lý: *"Nội dung do Trí tuệ nhân tạo tạo ra tự động, người dùng cần kiểm chứng lại trước khi áp dụng vào các quyết định quan trọng."* | `src/agentic/core/schemas.py` | Kiểm tra metadata của response API luôn chứa nhãn `ai_generated: true` và `disclaimer`. |
| **12.6** | `[ ]` | **P1** | **Bộ Lọc Độc Hại & Định Kiến (Fairness & Toxicity Guard)**: Output Guardrail tích hợp bộ lọc chặn ngôn từ kích động thù địch, xúc phạm danh dự, hoặc định kiến giới tính, chủng tộc, tôn giáo. | [src/agentic/safety/guardrails.py](../../src/agentic/safety/guardrails.py) | Chạy test an toàn đạo đức:<br>`pytest tests/unit/test_guardrails.py -k test_toxicity_filter` |

---

## 3. Quy Trình Xác Minh & Lệnh Thực Thi (Pre-Flight Runbook)

### Bước 1: Kiểm Thử Quy Trình Xóa Dữ Liệu Cá Nhân (Right to be Forgotten)
```bash
# Giả lập xóa dữ liệu của người dùng test_user_compliance_01
curl -s -X DELETE http://localhost:8000/v1/threads/compliance_test_thread \
  -H "Authorization: Bearer <ADMIN_TOKEN>" | jq .

# Xác nhận dữ liệu không còn tồn tại trong DB:
# SELECT COUNT(*) FROM checkpoints WHERE thread_id = 'compliance_test_thread'; -> Kết quả phải bằng 0.
```

### Bước 2: Kiểm Tra Trích Dẫn Nguồn RAG (Citations Verification)
```bash
curl -s -X POST http://localhost:8000/v1/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <USER_TOKEN>" \
  -d '{"message": "Chính sách nghỉ phép năm quy định thế nào?", "thread_id": "audit_rag_001"}' | jq .
# Xác nhận trong JSON trả về có trường: citations hoặc sources hợp lệ
```

---

## 4. Danh Mục Giám Sát Sau Triển Khai (Post-Deployment Hypercare)

Sau khi hệ thống vận hành chính thức:
- [ ] **T+1 Giờ**: Kiểm tra giao diện người dùng hiển thị đúng thông điệp AI Disclaimer và các phản hồi RAG có trích dẫn nguồn `citations` rõ ràng.
- [ ] **T+24 Giờ**: Rà soát audit log của các truy vấn nhạy cảm và quyết định HITL; kiểm tra ngẫu nhiên 20 phiên tương tác, đảm bảo không có rò rỉ thông tin định danh cá nhân (PII).
- [ ] **T+7 Ngày**: Đánh giá quy trình tiếp nhận và xử lý yêu cầu xóa dữ liệu người dùng (Right to be Forgotten) nếu có phát sinh trong thực tế.

---

## 5. Trạng Thái Nghiệm Thu & Liên Kết Ký Duyệt

- **Tổng số tiêu chí kiểm định**: 6 (P0: 4, P1: 2, P2: 0)
- **Đạt**: _____ / 6
- **Kết luận**: [ ] ĐẠT (PASS) &nbsp;&nbsp;&nbsp; [ ] CẦN KHẮC PHỤC (REJECT)

> 🎯 **Biên Bản Ký Duyệt**: Kết quả đánh giá của Trụ cột này được tổng hợp và ký duyệt chính thức tại [Bảng Chữ Ký Phê Chuẩn Của Hội Đồng Đánh Giá (Mục 6.3 - REVIEW.md)](../../.workflows/reviews/REVIEW.md#63-bảng-chữ-ký-phê-chuẩn-của-hội-đồng-đánh-giá).
