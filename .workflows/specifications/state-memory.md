# AGENTS_STATE_MEMORY.md — Quản Lý Bộ Nhớ & Trạng Thái (Memory & State)

> **Tài liệu tham chiếu chuyên sâu** thuộc phân vùng kiến trúc Multi-Agent trong hệ sinh thái `agentic-platform`.  
> Tài liệu gốc: [AGENTS.md](../../AGENTS.md) · [Architecture.md](../../Architecture.md).

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

### 2.1. Short-term Memory (`short_term.py`, `redis_saver.py`, `thread_registry.py`)
- Sử dụng LangGraph `AsyncPostgresSaver(conn=AsyncConnectionPool)` cho API async và `RedisSaver`. Mỗi phiên đàm thoại gắn với một `thread_id`.
- **PostgreSQL Connection Pool & PgBouncer**: Khởi tạo `AsyncConnectionPool` / `ConnectionPool` với `min_size=2, max_size=20`, gán `prepare_threshold=None` tương thích tuyệt đối với PgBouncer `POOL_MODE: transaction` (kèm `MAX_PREPARED_STATEMENTS: 100`), có kiểm tra sức khỏe qua `is_checkpointer_healthy()` trong `/readyz` (`readyz` cũng kiểm tra đồng thời `redis_url` và `redis_state_url`, fail-closed ở production).
- **Least-Privilege Database URIs & Migration Separation**: Tách `postgres_app_uri` (runtime DML cho API/Agents) khỏi `postgres_migration_uri` (`agentic-maintenance-secrets` cho `db-migration-job`). Runtime đặt `AUTO_RUN_MIGRATIONS=false`: production chỉ chạy kiểm tra read-only `check_database_migrations()` và fail-closed thay vì chạy DDL `saver.setup()` hay advisory lock dưới quyền `agentic_app`.
- **Tách Redis Cache (`REDIS_URL`) & Redis State (`REDIS_STATE_URL`)**: `redis-cache` (`allkeys-lru`) phục vụ RAG/Prompt cache và rate limiter; `redis-state` (`noeviction`) dành riêng cho `RedisSaver`, nonce chống replay, thread ownership (`ThreadRegistry`), JTI revocation và invalidation generation RAG. Bảo vệ dữ liệu checkpoint serialized bằng khóa `CHECKPOINT_HMAC_SECRET` bắt buộc riêng biệt với `S2S_INTERNAL_TOKEN` trên các role `api`/`maintenance`/`all`, và hỗ trợ xoay vòng khóa mượt mà thông qua `checkpoint_hmac_previous_secrets` trên `RedisSaver`, `ThreadRegistry` và `audit.py`.
- **Kubernetes Storage & Sync Ordering**: Các PVC `ReadWriteMany` (`agentic-rag-data`, `agentic-mcp-scratch`) gắn tường minh `storageClassName: efs-sc`; `db-migration-job.yaml` dùng ArgoCD Sync Waves (`-1` SA/SecretStore $\rightarrow$ `0` ConfigMap/datastores $\rightarrow$ `1` migration $\rightarrow$ `2` deployments $\rightarrow$ `3` bootstrap) thay cho `PreSync` hook.
- Cơ chế token trim giữ context window của prompt luôn nằm trong ngân sách an toàn.

### 2.2. Long-term Memory (`long_term.py`) & Immutable Audit (`audit.py`)
- Tái sử dụng `ConnectionPool` từ `short_term.py` qua `_get_pg_conn()`, loại bỏ chi phí bắt tay TCP/SSL trên mỗi transaction. Tìm kiếm full-text ranking qua Postgres `to_tsvector`/`plainto_tsquery` (được `planner.py` truy vấn trước khi lập kế hoạch) và dọn dữ liệu quá hạn theo chính sách `delete_older_than(max_age_days)` trong `scripts/cleanup_checkpoints.py`. Dead-letter fallback tự động lưu trữ tạm thời sang SQLite WAL khi PostgreSQL mất kết nối.
- **Immutable Audit Logs (`003_audit_logs.py`, `004_audit_logs_seq_and_checkpoints.py`)**: Chuỗi `prev_hash` + thứ tự đơn điệu `chain_seq` (`BigInteger` Identity) + `pg_advisory_xact_lock`, trigger Postgres ngăn chặn sửa/xóa (`trg_audit_logs_immutable`) và `REVOKE UPDATE, DELETE, TRUNCATE ON audit_logs FROM PUBLIC;`. Trên SQLite cũng duy trì `prev_hash` nối chuỗi đầy đủ; tính toàn vẹn chuỗi được kiểm chứng độc lập bằng `verify_audit_chain` / `scripts/verify_audit_chain.py` (hỗ trợ cả khóa hiện tại lẫn `checkpoint_hmac_previous_secrets`). Ghi log từ nhánh async qua `asyncio.to_thread(record_audit_event, ...)`.
- Lưu trữ trong cơ sở dữ liệu quan hệ kết hợp vector embeddings:
  - **Semantic**: Thông tin hồ sơ người dùng, các thực thể cố định.
  - **Episodic**: Bản ghi các tác vụ đã thực hiện thành công/thất bại trong quá khứ.
  - **Procedural**: Hướng dẫn, quy trình làm việc được tối ưu qua các lần phản hồi.
- **Quản lý tiến hóa lược đồ (Database Schema Migration)**: Toàn bộ bảng cơ sở dữ liệu PostgreSQL (`long_term_memories`, `checkpoints`, `checkpoint_migrations`, `checkpoint_blobs`, `checkpoint_writes` gồm `task_path`, `audit_logs` gồm `chain_seq`) được quản lý phiên bản nghiêm ngặt qua **Alembic** (`migrations/`). Bật `STRICT_MIGRATION=true` trong CI.
- **RAG Lifecycle, RTBF & Restore Drills (`data_lifecycle.py`, `ingestion.py`, `scripts/restore_drill.py`)**:
  - Xóa RTBF theo người dùng (`owner_id == user_id` trên Qdrant) tách biệt hoàn toàn với xóa corpus theo tổ chức (`purge_tenant_corpus=True, tenant_id=...`), đồng thời fan-out xóa key checkpoint/ownership/cache trên cả `redis-state` lẫn `redis-cache`. Sự kiện audit RTBF ghi chính xác `actor_id` của admin/operator thực thi và lưu `target_user_id` trong `details`.
  - Bootstrap chỉ mục ban đầu bằng `agentic-rag-bootstrap` (`HF_HOME=/opt/hf-cache`, `HF_HUB_OFFLINE=1`); cập nhật tăng trưởng từng file qua `ingest_single_document` / `delete_source` (tỉa leaf node cũ khỏi `docstore.json`) + endpoint nội bộ `POST /api/v1/admin/rag/cdc` với atomic persist và giữ nguyên `tenant_id`/ACL. Backup Qdrant định kỳ qua CronJob `agentic-qdrant-backup` trên PVC `agentic-qdrant-backups` (streaming SHA-256 + offsite upload fail-closed + giữ 14 ngày), kết hợp diễn tập kiểm chứng checksum và audit chain qua `scripts/restore_drill.py` (`--smoke`, `--pg-dump`, `--qdrant-snapshot`).

### 2.3. Consolidation Node (`consolidation.py`)
- Chạy sau bước `finalize` để chắt lọc thông tin quan trọng từ phiên hội thoại và ghi vào bộ nhớ dài hạn.

---

## 3. Tài Liệu Kiểm Định & Tham Chiếu Liên Quan

- **Kiểm định quản lý trạng thái, bộ nhớ & dữ liệu**: [REVIEW_STATE, MEMORY & PERSISTENCE.md](../reviews/REVIEW_STATE,%20MEMORY%20&%20PERSISTENCE.md)
- **Kiểm định quản trị dữ liệu & phân quyền tri thức**: [REVIEW_DATA GOVERNANCE & RAG RBAC.md](../reviews/REVIEW_DATA%20GOVERNANCE%20&%20RAG%20RBAC.md)
- **Kiểm định tuân thủ pháp lý & quyền được lãng quên (Right to be Forgotten)**: [REVIEW_COMPLIANCE, PRIVACY & AI ETHICS.md](../reviews/REVIEW_COMPLIANCE,%20PRIVACY%20&%20AI%20ETHICS.md)
# Bổ sung hardening persistence và tenant isolation

- Supervisor async sử dụng `AsyncPostgresSaver`; lifecycle connection được quản lý bởi FastAPI lifespan.
- Public `thread_id` được claim nguyên tử theo principal và chuyển thành HMAC-scoped checkpoint key trước khi truy cập state.
- Redis checkpoint chỉ deserialize payload có HMAC hợp lệ; legacy unsigned migration mặc định tắt và bị cấm ở production.
- HITL resume phải khớp đồng thời public thread, owner đã đăng ký và `step_id` của interrupt hiện tại.
