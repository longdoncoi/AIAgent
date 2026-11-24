# Troubleshooting đã kiểm chứng

## PgBouncer báo lỗi prepared statement

- **Triệu chứng**: `prepared statement already exists/does not exist` ở transaction pooling.
- **Fix**: giữ `prepare_threshold=None` trong PostgreSQL connections/pool; xem `src/agentic/memory/short_term.py` và `long_term.py`.

## Redis checkpoint bị từ chối

- **Triệu chứng**: HMAC mismatch hoặc payload cũ/không ký không deserialize được.
- **Fix**: không bypass validation; dùng cùng secret/format ở writer và reader, migrate hoặc xóa checkpoint cũ có chủ đích. Xem `redis_saver.py`.

## Async graph lỗi với checkpointer

- **Triệu chứng**: `ainvoke`/`astream_events` gọi async method chưa có.
- **Nguyên nhân**: compile graph async bằng saver đồng bộ.
- **Fix**: khởi tạo `AsyncPostgresSaver` trong FastAPI lifespan và giữ context đến shutdown.

## Event loop lag khi RAG lớn

- **Triệu chứng**: API latency tăng khi BM25, reranker hoặc ingestion chạy.
- **Fix**: đưa phần sync/CPU-bound qua worker thread/process; đo lại latency, không chỉ đổi cú pháp thành `async`.

## Approval không bảo vệ tool boundary

- **Triệu chứng**: step đã duyệt nhưng tool có thể bị gọi ngoài step hoặc dùng lại approval.
- **Fix**: capability phải khóa theo run/step/owner/agent/tool, có expiry và được kiểm ở A2A + MCP. Không tin metadata owner nếu capability invalid.

## Trùng `thread_id` giữa principal

- **Triệu chứng**: nguy cơ đọc/ghi chung checkpoint.
- **Fix**: resolve owner qua atomic registry và dùng internal checkpoint identity đã scope/ký; approval phải khớp pending step.

## CDC path traversal hoặc index stale

- **Triệu chứng**: input path thoát `data/raw` hoặc tài liệu sửa/xóa không phản ánh trong retrieval.
- **Fix**: `resolve()` + `relative_to()` dưới data root; xử lý upsert/delete, rebuild/invalidate index và reset retriever singleton.
