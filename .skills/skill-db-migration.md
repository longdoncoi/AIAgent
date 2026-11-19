# skill-db-migration.md — Quản Lý Database Schema & Alembic Migrations

> **Mục tiêu**: Hướng dẫn quy trình tiến hóa lược đồ cơ sở dữ liệu PostgreSQL cho L6 Memory và Checkpointer mà không làm gián đoạn hệ thống.

---

## 1. Các Lệnh Thường Dùng

```bash
# 1. Tạo script migration tự động dựa trên SQLAlchemy models
alembic revision --autogenerate -m "add_column_to_long_term_memory"

# 2. Áp dụng toàn bộ migration lên cơ sở dữ liệu
make db-upgrade
# tương đương: alembic upgrade head

# 3. Rollback 1 bước migration gần nhất
alembic downgrade -1

# 4. Kiểm tra lịch sử migration hiện tại
alembic current
alembic history --verbose
```

---

## 2. Quy Tắc An Toàn Cho Production

1. **Zero-Downtime Rule**: Tuyệt đối không xóa cột hoặc bảng đang có dữ liệu trong cùng một migration bước duy nhất. Sử dụng chiến lược Expand/Contract (thêm cột mới $\rightarrow$ migrate dữ liệu $\rightarrow$ bỏ cột cũ ở release tiếp theo).
2. **Checkpointer State**: Các bảng của LangGraph (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`) được quản lý schema tự động bởi PostgresSaver. Cẩn trọng khi can thiệp thủ công.
3. **PgBouncer Compatibility**: Các câu lệnh ALTER TABLE cần chú ý timeout lock để không làm tắc nghẽn connection pool.
