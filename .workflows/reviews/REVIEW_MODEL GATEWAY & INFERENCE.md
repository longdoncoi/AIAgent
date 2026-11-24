# REVIEW_MODEL GATEWAY & INFERENCE.md — Trụ Cột 5: Quản Lý Mô Hình & Cổng Phục Vụ

> **Tài liệu kiểm định chuyên biệt (Production Readiness Review - Pillar 05 / 14)**  
> **Đơn vị phụ trách**: AI Infrastructure Engineer & Model Serving Lead  
> **Tài liệu tham chiếu**: [REVIEW.md](../../.workflows/reviews/REVIEW.md) · [ARCHITECTURE.md](../../Architecture.md) · [AGENTS.md](../../AGENTS.md)

---

## 1. Mục Tiêu & Phạm Vi Kiểm Định

Trụ cột này đánh giá năng lực phục vụ mô hình ngôn ngữ lớn (LLM Serving) và cổng định tuyến Gateway:
1. **Định tuyến Mô hình (Model Routing & Fallback Chain)**: LiteLLM định tuyến chuẩn xác các alias `reasoning-llm`, `fast-llm`, `coding-llm`, `text-embedding-bge`. Tự động fallback từ GPU vLLM sang CPU llama.cpp.
2. **Cơ chế Tự Động Ngắt Mạch (Circuit Breaker)**: Ngắt kết nối và chuyển traffic tức thời khi GPU timeout liên tiếp mà không làm sập ứng dụng.
3. **Quản lý Hạn Mức Token (Token Budget & Quota)**: Kiểm soát mức tiêu thụ token trên từng lượt tương tác, phân chia ngân sách theo model alias.
4. **Đồng bộ Giới Hạn Context Window**: Đồng bộ tham số context window của vLLM (`32768`) và llama.cpp (`8192`) với cấu hình trim token của checkpointer.

---

## 2. Danh Mục Checklist Kiểm Tra Chi Tiết

| ID | Trạng Thái | Mức Độ | Tiêu Chí Kiểm Tra (Verification Criteria) | Tệp Mã Nguồn / Cấu Hình | Phương Pháp & Lệnh Kiểm Chứng |
| :---: | :---: | :---: | :--- | :--- | :--- |
| **5.1** | `[ ]` | **P0** | **Cấu hình Model Gateway LiteLLM**: Định tuyến chính xác các alias `reasoning-llm`, `fast-llm`, `coding-llm` và `text-embedding-bge`. Khởi tạo fallback chain từ vLLM (GPU) sang llama.cpp (CPU) khi GPU quá tải hoặc mất kết nối. | [configs/litellm_config.yaml](../../configs/litellm_config.yaml) | Kiểm tra health của LiteLLM:<br>`curl -f http://localhost:4000/health` |
| **5.2** | `[ ]` | **P0** | **Cơ chế Tự Động Ngắt Mạch (Circuit Breaker)**: Khi cụm GPU vLLM gặp sự cố hoặc timeout liên tiếp (quá 3 lần trong 30s), Gateway phải tự động kích hoạt Circuit Breaker, chuyển 100% traffic sang fallback CPU mà không làm crash API Supervisor. | [src/agentic/llm/gateway.py](../../src/agentic/llm/gateway.py) | Chạy chaos test giả lập ngắt vLLM:<br>`pytest tests/unit/test_production_hardening.py -k test_circuit_breaker` |
| **5.3** | `[ ]` | **P1** | **Kiểm Soát Hạn Mức Token (Token Budget & Quota)**: Kiểm soát và theo dõi token tiêu thụ theo từng lượt đàm thoại và phân bổ theo từng model alias, ngăn ngừa cạn kiệt tài nguyên hoặc quá tải chi phí. | [src/agentic/llm/gateway.py](../../src/agentic/llm/gateway.py) | Kiểm tra metrics token trong Prometheus:<br>`curl -s http://localhost:8000/metrics | grep agent_tokens_total` |
| **5.4** | `[ ]` | **P1** | **Context Window Limits Validation**: Đảm bảo các tham số context window của vLLM (`--max-model-len 32768`) và llama.cpp (`-c 8192`) được cấu hình đồng bộ với giới hạn trim của checkpointer. | `deploy/serving/vllm.sh`<br>`deploy/serving/llamacpp-chat.sh` | Kiểm tra tham số dòng lệnh khởi động serving scripts. |

---

## 3. Quy Trình Xác Minh & Lệnh Thực Thi (Pre-Flight Runbook)

### Bước 1: Kiểm Thử Circuit Breaker & Fallback Chain
```bash
pytest tests/unit/test_production_hardening.py -v -k "test_circuit_breaker or test_llm_gateway"
```

### Bước 2: Kiểm Tra Trạng Thái Sẵn Sàng Của LiteLLM Gateway
```bash
# 1. Health check LiteLLM
curl -s http://localhost:4000/health | jq .

# 2. Gửi test completion tới alias reasoning-llm:
curl -s http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-dummy-key" \
  -d '{
    "model": "reasoning-llm",
    "messages": [{"role": "user", "content": "ping"}],
    "max_tokens": 10
  }' | jq .
```

---

## 4. Danh Mục Giám Sát Sau Triển Khai (Post-Deployment Hypercare)

Sau khi hệ thống vận hành chính thức:
- [ ] **T+1 Giờ**: Kiểm tra tỷ lệ lỗi 5xx từ LiteLLM và upstream LLM backends $< 0.01\%$, P99 latency $< 5.0$ giây.
- [ ] **T+24 Giờ**: Đánh giá số lần Circuit Breaker kích hoạt và tỷ lệ chuyển hướng failover từ GPU vLLM sang CPU llama.cpp; đối soát context window limits không bị vượt ngưỡng.
- [ ] **T+7 Ngày**: Rà soát phân bổ lưu lượng theo model alias (`reasoning-llm`, `fast-llm`, `coding-llm`) và tối ưu hóa tham số batch size / max tokens dựa trên dữ liệu tải thực tế.

---

## 5. Trạng Thái Nghiệm Thu & Liên Kết Ký Duyệt

- **Tổng số tiêu chí kiểm định**: 4 (P0: 2, P1: 2, P2: 0)
- **Đạt**: _____ / 4
- **Kết luận**: [ ] ĐẠT (PASS) &nbsp;&nbsp;&nbsp; [ ] CẦN KHẮC PHỤC (REJECT)

> 🎯 **Biên Bản Ký Duyệt**: Kết quả đánh giá của Trụ cột này được tổng hợp và ký duyệt chính thức tại [Bảng Chữ Ký Phê Chuẩn Của Hội Đồng Đánh Giá (Mục 6.3 - REVIEW.md)](../../.workflows/reviews/REVIEW.md#63-bảng-chữ-ký-phê-chuẩn-của-hội-đồng-đánh-giá).
