# REVIEW_DATA GOVERNANCE & RAG RBAC.md — Trụ Cột 14: Quản Trị Dữ Liệu & Phân Quyền Tri Thức Cấp Tài Liệu

> **Tài liệu kiểm định chuyên biệt (Production Readiness Review - Pillar 14 / 14)**  
> **Đơn vị phụ trách**: Data Governance Lead & Security Architect & Lead RAG Engineer  
> **Tài liệu tham chiếu**: [REVIEW.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW.md) · [Architecture.md](file:///f:/PROJECTS/AIAgent/Architecture.md) · [AGENTS.md](file:///f:/PROJECTS/AIAgent/AGENTS.md) · [src/agentic/rag/](file:///f:/PROJECTS/AIAgent/src/agentic/rag/) · [scripts/ingest.py](file:///f:/PROJECTS/AIAgent/scripts/ingest.py)

---

## 1. Mục Tiêu & Phạm Vi Kiểm Định

Trụ cột này bảo đảm kho tri thức doanh nghiệp được quản trị chặt chẽ, an toàn và phân quyền chính xác:
1. **Phân Quyền Tri Thức Cấp Tài Liệu (Document-Level Security / Row-Level RBAC)**: Ngăn chặn rò rỉ thông tin mật chéo phòng ban. Người dùng thuộc phòng ban nào (HR, Kế toán, Công nghệ) chỉ được RAG truy xuất các tài liệu được gán nhãn quyền hạn tương ứng.
2. **Khử Trùng Lặp & Vệ Sinh Tri Thức (Deduplication & Chunk Hygiene)**: Thuật toán nạp tài liệu tự động phát hiện và loại bỏ các chunk trùng lặp (hash check), tránh làm nhiễu không gian vector và giảm chất lượng tìm kiếm.
3. **Quản Lý Vòng Đời & Thu Hồi Tri Thức Hết Hạn (Deprecation & Vector Invalidation)**: Khi một chính sách bị thu hồi hoặc thay thế, hệ thống phải tự động xóa hoặc gắn cờ vô hiệu hóa (`is_active: false`) trên Vector DB và Knowledge Graph, ngăn chặn cung cấp thông tin sai lệch.
4. **Đồng Bộ Dữ Liệu Thời Gian Thực (Realtime CDC Ingestion via Temporal)**: Khi có tài liệu mới được tải lên kho lưu trữ doanh nghiệp, pipeline Temporal tự động phân tích cú pháp (Parent-Child 1024/256), tạo embedding và cập nhật chỉ mục trong vòng $< 60$ giây.
5. **Cam Kết Độ Tươi Mới Tri Thức (Data Freshness SLA)**: Thời gian từ khi tài liệu được ban hành đến khi Agent có thể trả lời chính xác không vượt quá thời hạn cam kết nghiệp vụ.

---

## 2. Danh Mục Checklist Kiểm Tra Chi Tiết

| ID | Trạng Thái | Mức Độ | Tiêu Chí Kiểm Tra (Verification Criteria) | Tệp Mã Nguồn / Cấu Hình | Phương Pháp & Lệnh Kiểm Chứng |
| :---: | :---: | :---: | :--- | :--- | :--- |
| **14.1** | **IMPLEMENTED — PENDING STAGING VERIFICATION** | **P0** | **Phân Quyền Truy Cập Tri Thức Cấp Tài Liệu (Document-Level RBAC)**: Bộ lọc metadata filtering trên Vector Store và Knowledge Graph bắt buộc kiểm tra `tenant_id`, `user_roles`, `department` và ACL của người dùng trước khi trả về các chunk ngữ cảnh, triệt tiêu rủi ro lộ bí mật kinh doanh chéo bộ phận. | [src/agentic/rag/retriever.py](file:///f:/PROJECTS/AIAgent/src/agentic/rag/retriever.py)<br>[src/agentic/rag/tools.py](file:///f:/PROJECTS/AIAgent/src/agentic/rag/tools.py)<br>[src/agentic/rag/kg.py](file:///f:/PROJECTS/AIAgent/src/agentic/rag/kg.py) | `pytest tests/unit/test_rag_hardening_v3.py -v`; sau đó probing chéo tenant/role trên Qdrant staging. |
| **14.2** | `[ ]` | **P0** | **Tự Động Thu Hồi & Hủy Chỉ Mục Tài Liệu Hết Hạn (Vector Invalidation)**: Khi tài liệu bị đánh dấu thu hồi (deprecated), workflow Temporal (`rag_workflow.py`) phải tự động xóa bỏ các vector embeddings liên quan khỏi Qdrant và làm mới Semantic Cache. | [src/agentic/workflows/rag_workflow.py](file:///f:/PROJECTS/AIAgent/src/agentic/workflows/rag_workflow.py)<br>[src/agentic/cache/rag_cache.py](file:///f:/PROJECTS/AIAgent/src/agentic/cache/rag_cache.py) | Chạy test xóa tài liệu và kiểm chứng không còn tìm thấy trong vector store:<br>`pytest tests/unit/test_ten_out_of_ten.py -k test_rag_workflow_invalidation` |
| **14.3** | `[ ]` | **P1** | **Khử Trùng Lặp & Vệ Sinh Chunk (Deduplication & Hygiene)**: Script nạp tri thức (`scripts/ingest.py`) kiểm tra hash MD5/SHA256 của từng file và nội dung chunk, loại bỏ các đoạn văn trùng lặp trước khi tính toán embeddings. | [scripts/ingest.py](file:///f:/PROJECTS/AIAgent/scripts/ingest.py)<br>[src/agentic/rag/ingestion.py](file:///f:/PROJECTS/AIAgent/src/agentic/rag/ingestion.py) | Chạy ingest với tài liệu trùng và xác nhận số lượng vector không bị nhân bản. |
| **14.4** | `[ ]` | **P1** | **Tính Nhất Quán & Đồng Bộ Thời Gian Thực (Temporal CDC)**: Quy trình cập nhật chỉ mục xử lý bền bỉ qua Temporal Activity; nếu quá trình embed gặp sự cố mạng, Temporal tự động retry với exponential backoff mà không làm mất dữ liệu. | [src/agentic/workflows/rag_workflow.py](file:///f:/PROJECTS/AIAgent/src/agentic/workflows/rag_workflow.py) | Kiểm tra test retry activity của Temporal ingest. |
| **14.5** | `[ ]` | **P2** | **Cam Kết Độ Tươi Mới Dữ Liệu (Data Freshness SLA)**: Tài liệu được nạp hoàn tất và sẵn sàng cho Agent trả lời trong vòng tối đa 60 giây sau khi upload. | `scripts/ingest.py` | Đo thời gian end-to-end từ khi nạp file vào `data/raw/` đến khi xuất hiện trong index `data/index/`. |

### Thực Trạng Triển Khai (Implementation Truth)

- **14.1 — IMPLEMENTED, CHỜ KIỂM CHỨNG STAGING**: JWT claims được API pin vào metadata có chữ ký A2A, sau đó chuyển thành `ToolExecutionContext`. `search_policy` loại bỏ auth filter do caller tự khai báo; retriever push down tenant/role/department/ACL xuống Qdrant và lọc phòng thủ lần hai. Knowledge Graph dùng node id có namespace tenant và áp dụng cùng chính sách. Regression test bao phủ role, department, ACL và caller override. Trạng thái vẫn chưa được tự động gán PASS trước khi probing trên hạ tầng staging thật.

---

## 3. Quy Trình Xác Minh & Lệnh Thực Thi (Pre-Flight Runbook)

### Bước 1: Kiểm Thử Ingestion & Khử Trùng Lặp
```bash
# Nạp tài liệu nguồn vào vector store và kiểm tra output
python scripts/ingest.py --data-dir data/raw --index-dir data/index
```

### Bước 2: Kiểm Thử Thu Hồi Tài Liệu & Xóa Cache Tự Động
```bash
pytest tests/unit/test_ten_out_of_ten.py -v -k "test_rag_workflow_invalidation"
```

### Bước 3: Kiểm Tra Lọc Metadata Phân Quyền Trong Retriever
```bash
pytest tests/unit/test_rag_hardening_v3.py -v
```

---

## 4. Danh Mục Giám Sát Sau Triển Khai (Post-Deployment Hypercare)

Sau khi hệ thống vận hành chính thức:
- [ ] **T+1 Giờ**: Xác nhận Temporal CDC pipeline nạp tài liệu mới hoạt động trơn tru trong thời gian cam kết ($< 60$ giây).
- [ ] **T+24 Giờ**: Kiểm toán việc thực thi phân quyền cấp tài liệu (Document-Level RBAC), bảo đảm không có trường hợp truy xuất chéo dữ liệu mật giữa các phòng ban.
- [ ] **T+7 Ngày**: Rà soát và thu hồi các vector embeddings của các tài liệu chính sách đã hết hiệu lực; xác nhận không có chunk trùng lặp hoặc orphan vectors tồn đọng trong Qdrant.

---

## 5. Trạng Thái Nghiệm Thu & Liên Kết Ký Duyệt

- **Tổng số tiêu chí kiểm định**: 5 (P0: 2, P1: 2, P2: 1)
- **Đạt**: _____ / 5
- **Kết luận**: [ ] ĐẠT (PASS) &nbsp;&nbsp;&nbsp; [ ] CẦN KHẮC PHỤC (REJECT)

> 🎯 **Biên Bản Ký Duyệt**: Kết quả đánh giá của Trụ cột này được tổng hợp và ký duyệt chính thức tại [Bảng Chữ Ký Phê Chuẩn Của Hội Đồng Đánh Giá (Mục 6.3 - REVIEW.md)](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW.md#63-bảng-chữ-ký-phê-chuẩn-của-hội-đồng-đánh-giá).

---

## 6. Bằng Chứng Kiểm Định Tự Động (Auto-Evidence Status)

> **AUTO-EVIDENCE PENDING** — chờ đối soát và **ký bởi Release Board**. Không có nhãn `PASSED` được tự động gán cho tài liệu này.

- Artifact: [evidence/prr/latest.json](file:///f:/PROJECTS/AIAgent/evidence/prr/latest.json) (`commit_sha`, `timestamp_utc`, exit code của `ruff` / `mypy` / `pytest --collect-only` / `eval --mode smoke` / parse YAML).
- Sinh artifact: `python scripts/generate_prr_evidence.py`. Tất cả ô trạng thái `[]` ở các mục trên vẫn giữ nguyên cho đến khi Hội đồng đối soát artifact và ký duyệt.
- Bảng chữ ký 9 đại diện: [REVIEW.md mục 6.3](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW.md#63-bảng-chữ-ký-phê-chuẩn-của-hội-đồng-đánh-giá).
