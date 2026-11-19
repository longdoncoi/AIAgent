# REVIEW_STATE, MEMORY & PERSISTENCE.md — Trụ Cột 2: Quản Lý Trạng Thái, Bộ Nhớ & Dữ Liệu

> **Tài liệu kiểm định chuyên biệt (Production Readiness Review - Pillar 02 / 14)**  
> **Đơn vị phụ trách**: Lead Backend Engineer & Database Administrator (DBA)  
> **Tài liệu tham chiếu**: [REVIEW.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW.md) · [Architecture.md](file:///f:/PROJECTS/AIAgent/Architecture.md) · [AGENTS.md](file:///f:/PROJECTS/AIAgent/AGENTS.md)

---

## 1. Mục Tiêu & Phạm Vi Kiểm Định

Trụ cột này thẩm định độ bền vững, tính toàn vẹn và an toàn của hệ thống quản lý bộ nhớ 2 tầng (Short-term & Long-term):
1. **Khả năng mở rộng kết nối CSDL**: Tương thích tuyệt đối giữa SQLAlchemy/asyncpg với PgBouncer ở chế độ `transaction`, thiết lập `prepare_threshold=None`.
2. **Bảo vệ Bộ Nhớ Ngắn Hạn Phân Tán**: Sử dụng RedisSaver bất đồng bộ, có cơ chế xác thực chữ ký HMAC-SHA256 để chống can thiệp hoặc giải mã dữ liệu độc hại (tampering).
3. **Quản lý Vòng Đời Dữ Liệu & Context Window**: Cắt tỉa (trim) token tự động trong ngân sách an toàn, áp dụng TTL tự động dọn dẹp các checkpoint cũ trên Redis.
4. **Bộ Nhớ Dài Hạn Bền Vững (LTM)**: Tái sử dụng pool kết nối, có dead-letter fallback sang SQLite WAL khi PostgreSQL mất kết nối.
5. **Tiến Hóa Lược Đồ CSDL (Database Migrations)**: Quản lý lược đồ CSDL tập trung qua Alembic, bảo đảm tính toàn vẹn và khả năng rollback zero-downtime.

---

## 2. Danh Mục Checklist Kiểm Tra Chi Tiết

| ID | Trạng Thái | Mức Độ | Tiêu Chí Kiểm Tra (Verification Criteria) | Tệp Mã Nguồn / Cấu Hình | Phương Pháp & Lệnh Kiểm Chứng |
| :---: | :---: | :---: | :--- | :--- | :--- |
| **2.1** | `[ ]` | **P0** | **Tương Thích PgBouncer & Tránh Lỗi Prepared Statements**: Thiết lập `prepare_threshold=None` trong connection pool Postgres, loại bỏ hoàn toàn lỗi duplicate statement name khi scale qua PgBouncer transaction pooling. | [src/agentic/memory/short_term.py](file:///f:/PROJECTS/AIAgent/src/agentic/memory/short_term.py) | Chạy unit test kiểm tra pool Postgres:<br>`pytest tests/unit/test_ten_out_of_ten_hardening.py -k test_pgbouncer_compatibility` |
| **2.2** | `[ ]` | **P0** | **Chữ Ký Toàn Vẹn Dữ Liệu Redis (HMAC-SHA256)**: Kiểm tra chữ ký HMAC trên toàn bộ payload tuần tự hóa trước khi nạp vào RedisSaver, ngăn ngừa tuyệt đối tấn công deserialization injection. | [src/agentic/memory/redis_saver.py](file:///f:/PROJECTS/AIAgent/src/agentic/memory/redis_saver.py) | Chạy unit test bảo mật chữ ký HMAC:<br>`pytest tests/unit/test_caching_and_security.py -k test_redis_saver_hmac_tampering` |
| **2.3** | `[ ]` | **P0** | **Đồng Bộ Quản Trị Lược Đồ CSDL (Alembic Migrations)**: Toàn bộ bảng hệ thống (`long_term_memories`, `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`) được quản lý qua migration script; đã test nâng cấp và rollback. | [migrations/](file:///f:/PROJECTS/AIAgent/migrations/)<br>[scripts/migrate_db.py](file:///f:/PROJECTS/AIAgent/scripts/migrate_db.py) | Chạy dry-run migration:<br>`alembic upgrade head --sql` |
| **2.4** | `[ ]` | **P1** | **Thời Hạn Sống Dữ Liệu & Context Trim (Token Budget Trim)**: Khởi tạo cơ chế trim message tokens theo ngưỡng cấu hình (`max_tokens=4000`), kèm TTL tự động xóa checkpoints quá hạn trên Redis (mặc định 7 ngày). | [src/agentic/memory/short_term.py](file:///f:/PROJECTS/AIAgent/src/agentic/memory/short_term.py) | Chạy unit test token trim:<br>`pytest tests/unit/test_production_hardening.py -k test_token_trimming` |
| **2.5** | `[ ]` | **P1** | **Tái Sử Dụng Connection Pool Cho LTM (No Connection Leaks)**: `long_term.py` phải tái sử dụng `_get_pg_conn()` từ pool của `short_term.py`, không tạo kết nối TCP mới cho mỗi transaction. | [src/agentic/memory/long_term.py](file:///f:/PROJECTS/AIAgent/src/agentic/memory/long_term.py) | Kiểm tra mã nguồn và chạy stress test kiểm tra số lượng connection mở trong Postgres. |
| **2.6** | `[ ]` | **P1** | **Cơ Chế Dead-Letter Fallback Khi Postgres Mất Kết Nối**: Khi Postgres gián đoạn kết nối, LTM tự động ghi nhận dữ liệu vào local SQLite WAL buffer và đồng bộ lại khi Postgres online. | [src/agentic/memory/long_term.py](file:///f:/PROJECTS/AIAgent/src/agentic/memory/long_term.py) | Chạy chaos test mô phỏng mất kết nối DB:<br>`pytest tests/unit/test_ten_out_of_ten_hardening.py -k test_ltm_dead_letter_fallback` |

---

## 3. Quy Trình Xác Minh & Lệnh Thực Thi (Pre-Flight Runbook)

### Bước 1: Kiểm Tra Migration Alembic
```bash
# Kiểm tra migration status và upgrade lên head
alembic current
alembic upgrade head
python scripts/migrate_db.py
```

### Bước 2: Chạy Bộ Test Bảo Vệ Bộ Nhớ & Chống Tampering
```bash
pytest tests/unit/test_caching_and_security.py -v -k "test_redis_saver or test_hmac"
pytest tests/unit/test_ten_out_of_ten_hardening.py -v -k "test_pgbouncer"
```

### Bước 3: Xác Thực Trí Nhớ Dài Hạn (LTM Consolidation Node)
```bash
pytest tests/unit/test_production_hardening.py -v -k "test_ltm or test_consolidation"
```

---

## 4. Danh Mục Giám Sát Sau Triển Khai (Post-Deployment Hypercare)

Sau khi hệ thống tiếp nhận lưu lượng truy cập thực tế:
- [ ] **T+1 Giờ**: Kiểm tra số lượng kết nối đang hoạt động trong PgBouncer pool (`active_clients`, `active_servers`), bảo đảm không có hiện tượng rò rỉ kết nối (connection leak).
- [ ] **T+24 Giờ**: Đánh giá độ ổn định của PostgreSQL Connection Pool (số lượng kết nối hoạt động qua PgBouncer ở chế độ `transaction` duy trì trong định mức 2-20).
- [ ] **T+24 Giờ**: Kiểm tra dung lượng bảng `checkpoints` và `checkpoint_blobs`, xác nhận cơ chế TTL dọn dẹp hoạt động bình thường và chữ ký HMAC-SHA256 trên Redis không phát sinh lỗi validation.

---

## 5. Trạng Thái Nghiệm Thu & Liên Kết Ký Duyệt

- **Tổng số tiêu chí kiểm định**: 6 (P0: 3, P1: 3, P2: 0)
- **Đạt**: _____ / 6
- **Kết luận**: [ ] ĐẠT (PASS) &nbsp;&nbsp;&nbsp; [ ] CẦN KHẮC PHỤC (REJECT)

> 🎯 **Biên Bản Ký Duyệt**: Kết quả đánh giá của Trụ cột này được tổng hợp và ký duyệt chính thức tại [Bảng Chữ Ký Phê Chuẩn Của Hội Đồng Đánh Giá (Mục 6.3 - REVIEW.md)](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW.md#63-bảng-chữ-ký-phê-chuẩn-của-hội-đồng-đánh-giá).
