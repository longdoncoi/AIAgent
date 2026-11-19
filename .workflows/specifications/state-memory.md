# AGENTS_STATE_MEMORY.md — Quản Lý Bộ Nhớ & Trạng Thái (Memory & State)

> **Tài liệu tham chiếu chuyên sâu** thuộc phân vùng kiến trúc Multi-Agent trong hệ sinh thái `agentic-platform`.  
> Tài liệu gốc: [AGENTS.md](file:///f:/PROJECTS/AIAgent/AGENTS.md) · [Architecture.md](file:///f:/PROJECTS/AIAgent/Architecture.md).

---

## 1. Sơ Đồ Kiến Trúc Phân Tầng Bộ Nhớ (L6 Memory Subsystem)

```
                       ┌─────────────────────────┐
                       │   Request từ Người Dùng │
                       └────────────┬────────────┘
                                    │
                                    ▼
       ┌────────────────────────────────────────────────────────┐
       │                  L6 Memory Subsystem                   │
       ├────────────────────────────┬───────────────────────────┤
       │ Short-term Memory          │ Long-term Store (Postgres)│
       │ - Postgres Checkpointer    │ - Semantic (Fact/Profile) │
       │ - Thread ID & Step State   │ - Episodic (Lịch sử task) │
       │ - Trim window tokens       │ - Procedural (Kinh nghiệm)│
       └──────────────┬─────────────┴─────────────┬─────────────┘
                      │                           │
                      ▼                           ▼
       ┌────────────────────────┐   ┌───────────────────────────┐
       │ LangGraph Active State │   │ Node Consolidation        │
       │ (Phục vụ phiên hiện tại│   │ (Đúc kết lưu dài hạn sau  │
       │  hỗ trợ resume/replay) │   │  khi task kết thúc)       │
       └────────────────────────┘   └───────────────────────────┘
```

---

## 2. Chi Tiết Các Tầng Bộ Nhớ

### 2.1. Short-term Memory (`short_term.py`, `redis_saver.py`)
- Sử dụng LangGraph `PostgresSaver` hoặc `RedisSaver`. Mỗi phiên đàm thoại gắn với một `thread_id`.
- **PostgreSQL Connection Pool & PgBouncer**: Khởi tạo `ConnectionPool` với `min_size=2, max_size=20`, gán `prepare_threshold=None` tương thích tuyệt đối với PgBouncer `POOL_MODE: transaction` (kèm `MAX_PREPARED_STATEMENTS: 100`).
- **Distributed Async RedisSaver**: Hỗ trợ native I/O bất đồng bộ (`aput`, `aget_tuple` dùng `redis.asyncio`) song song cùng API đồng bộ. Tích hợp mã hoá và xác thực chữ ký **HMAC-SHA256** trên dữ liệu serialized, ngăn chặn triệt để lỗ hổng tamper/deserialization.
- Cơ chế token trim giữ context window của prompt luôn nằm trong ngân sách an toàn.

### 2.2. Long-term Memory (`long_term.py`)
- Tái sử dụng `ConnectionPool` từ `short_term.py` qua `_get_pg_conn()`, loại bỏ chi phí bắt tay TCP/SSL trên mỗi transaction. Dead-letter fallback tự động lưu trữ tạm thời sang SQLite WAL khi PostgreSQL mất kết nối.
- Lưu trữ trong cơ sở dữ liệu quan hệ kết hợp vector embeddings:
  - **Semantic**: Thông tin hồ sơ người dùng, các thực thể cố định.
  - **Episodic**: Bản ghi các tác vụ đã thực hiện thành công/thất bại trong quá khứ.
  - **Procedural**: Hướng dẫn, quy trình làm việc được tối ưu qua các lần phản hồi.
- **Quản lý tiến hóa lược đồ (Database Schema Migration)**: Toàn bộ bảng cơ sở dữ liệu PostgreSQL (`long_term_memories`, `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`) được quản lý phiên bản nghiêm ngặt qua **Alembic** (`migrations/`). Tự động nâng cấp lược đồ khi deploy và hỗ trợ rollback zero-downtime.

### 2.3. Consolidation Node (`consolidation.py`)
- Chạy sau bước `finalize` để chắt lọc thông tin quan trọng từ phiên hội thoại và ghi vào bộ nhớ dài hạn.

---

## 3. Tài Liệu Kiểm Định & Tham Chiếu Liên Quan

- **Kiểm định quản lý trạng thái, bộ nhớ & dữ liệu**: [REVIEW_STATE, MEMORY & PERSISTENCE.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_STATE,%20MEMORY%20&%20PERSISTENCE.md)
- **Kiểm định quản trị dữ liệu & phân quyền tri thức**: [REVIEW_DATA GOVERNANCE & RAG RBAC.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_DATA%20GOVERNANCE%20&%20RAG%20RBAC.md)
- **Kiểm định tuân thủ pháp lý & quyền được lãng quên (Right to be Forgotten)**: [REVIEW_COMPLIANCE, PRIVACY & AI ETHICS.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW_COMPLIANCE,%20PRIVACY%20&%20AI%20ETHICS.md)
