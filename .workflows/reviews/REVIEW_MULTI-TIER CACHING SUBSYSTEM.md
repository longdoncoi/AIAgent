# REVIEW_MULTI-TIER CACHING SUBSYSTEM.md — Trụ Cột 3: Hệ Thống Bộ Đệm Đa Tầng

> **Tài liệu kiểm định chuyên biệt (Production Readiness Review - Pillar 03 / 14)**  
> **Đơn vị phụ trách**: Performance Lead & Lead Backend Engineer  
> **Tài liệu tham chiếu**: [REVIEW.md](../../.workflows/reviews/REVIEW.md) · [ARCHITECTURE.md](../../Architecture.md) · [AGENTS.md](../../AGENTS.md)

---

## 1. Mục Tiêu & Phạm Vi Kiểm Định

Trụ cột này đánh giá hiệu năng, tính nhất quán và cơ chế giảm tải của hệ thống bộ đệm đa tầng (Multi-Tier Caching):
1. **Kiến Trúc Hai Tầng (Two-Tier L1/L2 Cache)**: L1 in-memory LRU cục bộ trong Pod, L2 Redis phân tán dùng chung cho cụm microservices.
2. **Bộ đệm Tri thức RAG (RAG Semantic & Query Cache)**: Cache kết quả tìm kiếm và reranking, giảm tải truy vấn lặp lại vào Vector Store.
3. **Bộ đệm Phản hồi Prompt (Prompt & Completion Cache)**: Cache các phản hồi LLM mang tính tất định, tối ưu chi phí token và độ trễ phản hồi.
4. **Tối ưu hóa GPU KV-Cache (Automatic Prefix Caching - APC)**: Ghim cấu trúc prompt tĩnh lên đầu, khai thác triệt để bộ đệm KV của vLLM và llama.cpp.
5. **Cơ chế Vô hiệu hóa Bền vững (Cache Invalidation via Temporal CDC)**: Tự động xóa hoặc làm mới cache khi tri thức nền tảng thay đổi.

---

## 2. Danh Mục Checklist Kiểm Tra Chi Tiết

| ID | Trạng Thái | Mức Độ | Tiêu Chí Kiểm Tra (Verification Criteria) | Tệp Mã Nguồn / Cấu Hình | Phương Pháp & Lệnh Kiểm Chứng |
| :---: | :---: | :---: | :--- | :--- | :--- |
| **3.1** | `[ ]` | **P0** | **Kiến Trúc Bộ Đệm 2 Tầng (Two-Tier L1/L2 Caching)**: Cả RAG Cache và Prompt Cache phải triển khai mô hình 2 tầng: L1 In-Memory LRU (phản hồi $< 1$ms) kết hợp L2 Redis phân tán. Tự động thăng cấp (promote) dữ liệu từ L2 lên L1 khi cache hit. | [src/agentic/cache/rag_cache.py](../../src/agentic/cache/rag_cache.py)<br>[src/agentic/cache/prompt_cache.py](../../src/agentic/cache/prompt_cache.py) | Chạy unit test bộ đệm 2 tầng:<br>`pytest tests/unit/test_caching_and_security.py -k test_two_tier` |
| **3.2** | `[ ]` | **P0** | **Tối Ưu KV-Cache GPU (Automatic Prefix Caching - APC)**: System message phải là chuỗi tĩnh ghim ở index 0 (không chứa timestamp/UUID động). Request gắn header cache phù hợp (`X-Cache-Control: ephemeral`) để router vLLM/SGLang tận dụng bộ nhớ GPU. | [src/agentic/cache/kv_cache.py](../../src/agentic/cache/kv_cache.py) | Chạy unit test kiểm tra prompt format cho KV-cache:<br>`pytest tests/unit/test_caching_and_security.py -k test_kv_cache_optimizer` |
| **3.3** | `[ ]` | **P1** | **Vô Hiệu Hóa Cache Hướng Sự Kiện (CDC Invalidation)**: Khi tài liệu RAG cập nhật hoặc bị thu hồi, workflow Temporal (`rag_workflow.py`) phải tự động kích hoạt xóa cache tương ứng trên Redis cluster mà không cần restart services. | [src/agentic/workflows/rag_workflow.py](../../src/agentic/workflows/rag_workflow.py) | Kiểm tra test vô hiệu hóa cache khi tài liệu thay đổi:<br>`pytest tests/unit/test_ten_out_of_ten.py -k test_rag_workflow_invalidation` |
| **3.4** | `[ ]` | **P1** | **Chỉ Số Đo Lường Hiệu Suất Bộ Đệm (Cache Hit/Miss Metrics)**: Tích hợp đầy đủ Prometheus metrics: `cache_hits_total`, `cache_misses_total` phân biệt theo từng tầng L1 và L2. | [src/agentic/observability/metrics.py](../../src/agentic/observability/metrics.py) | Xác thực các nhãn metrics trên endpoint `/metrics`:<br>`curl -s http://localhost:8000/metrics | grep cache_` |

---

## 3. Quy Trình Xác Minh & Lệnh Thực Thi (Pre-Flight Runbook)

### Bước 1: Kiểm thử Toàn Diện Caching Subsystem
```bash
pytest tests/unit/test_caching_and_security.py -v -k "test_rag_cache or test_prompt_cache or test_two_tier or test_kv_cache"
```

### Bước 2: Kiểm thử Đo Lường Tỷ Lệ Hit/Miss Metrics Trong Prometheus
```bash
# Gửi 2 truy vấn giống nhau để kiểm chứng Cache Hit:
curl -s -X POST http://localhost:8000/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Kiến trúc tổng thể của hệ thống gồm những tầng nào?", "thread_id": "cache_test_01"}' | jq .

curl -s -X POST http://localhost:8000/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Kiến trúc tổng thể của hệ thống gồm những tầng nào?", "thread_id": "cache_test_02"}' | jq .

# Kiểm tra metrics
curl -s http://localhost:8000/metrics | grep "cache_hits_total"
```

---

## 4. Danh Mục Giám Sát Sau Triển Khai (Post-Deployment Hypercare)

Sau khi hệ thống tiếp nhận lưu lượng truy cập thực tế:
- [ ] **T+1 Giờ**: Xác nhận tỷ lệ Cache Hit của RAG Cache L1/L2 đạt tối thiểu $\ge 40\%$ trên lưu lượng truy vấn thực tế của người dùng.
- [ ] **T+24 Giờ**: Đánh giá tỷ lệ Cache Hit duy trì ổn định $\ge 65\%$, giảm thiểu 70-80% truy vấn trực tiếp vào Vector Store và tiết kiệm 30-40% lượng token tiêu thụ.
- [ ] **T+24 Giờ**: Xác nhận KV-cache GPU (APC) duy trì hit rate cao trên các `SystemMessage` tĩnh của vLLM.
- [ ] **T+7 Ngày**: Kiểm tra dung lượng RAM của Redis Cache cluster, xác nhận chính sách LRU eviction hoạt động trơn tru không gây tràn bộ nhớ.

---

## 5. Trạng Thái Nghiệm Thu & Liên Kết Ký Duyệt

- **Tổng số tiêu chí kiểm định**: 4 (P0: 2, P1: 2, P2: 0)
- **Đạt**: _____ / 4
- **Kết luận**: [ ] ĐẠT (PASS) &nbsp;&nbsp;&nbsp; [ ] CẦN KHẮC PHỤC (REJECT)

> 🎯 **Biên Bản Ký Duyệt**: Kết quả đánh giá của Trụ cột này được tổng hợp và ký duyệt chính thức tại [Bảng Chữ Ký Phê Chuẩn Của Hội Đồng Đánh Giá (Mục 6.3 - REVIEW.md)](../../.workflows/reviews/REVIEW.md#63-bảng-chữ-ký-phê-chuẩn-của-hội-đồng-đánh-giá).
