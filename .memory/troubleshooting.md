# troubleshooting.md — Sổ Tay Xử Lý Lỗi & Bẫy Thường Gặp (Gotchas)

> Tệp này ghi lại các sự cố thực tế, các cạm bẫy đã gặp trong quá trình phát triển dự án và giải pháp đã kiểm chứng.

---

## 1. Lỗi: PgBouncer "Prepared Statement Already Exists" Hoặc "Prepared Statement Does Not Exist"
- **Hiện tượng**: Khi chạy connection pool kết nối tới PostgreSQL thông qua PgBouncer ở chế độ `transaction pooling`, phát sinh lỗi liên quan đến prepared statements.
- **Nguyên nhân**: PgBouncer ở transaction mode chia sẻ kết nối giữa các transaction khác nhau, prepared statement tạo ở connection này không tồn tại hoặc bị trùng lặp ở connection khác.
- **Giải pháp**:
  - Trong `short_term.py` và `long_term.py`, khi khởi tạo `ConnectionPool`, thiết lập `prepare_threshold=None`.
  - Cấu hình PgBouncer `POOL_MODE: transaction` kết hợp `MAX_PREPARED_STATEMENTS: 100`.

---

## 2. Lỗi: Redis Checkpointer Deserialization Security Alert
- **Hiện tượng**: Kiểm tra an ninh cảnh báo rủi ro deserialization giả mạo (tampering) trên Redis cache lưu state của LangGraph.
- **Nguyên nhân**: State checkpoint serialize bằng pickle hoặc JSON có thể bị can thiệp nếu hạ tầng mạng Redis bị xâm nhập.
- **Giải pháp**:
  - Trong `redis_saver.py`, bổ sung lớp xác thực chữ ký số **HMAC-SHA256** trên dữ liệu serialized trước khi `SET` vào Redis.
  - Khi đọc (`GET`), xác thực lại chữ ký HMAC trước khi deserialize. Nếu chữ ký không khớp, từ chối nạp và ghi log security incident.

---

## 3. Lỗi: Event Loop Lagging Khi Query RAG Lớn
- **Hiện tượng**: Khi RAGAgent thực hiện BM25 lexical search hoặc Cross-Encoder reranking trên tập tài liệu lớn, toàn bộ API L1 bị giật lag và response time tăng đột biến.
- **Nguyên nhân**: BM25 và Cross-Encoder chạy đồng bộ trên main thread, chiếm dụng Python GIL và làm nghẽn asyncio event loop.
- **Giải pháp**:
  - Chuyển toàn bộ lời gọi CPU-bound sang worker thread pool bằng `asyncio.to_thread()`.
# Hardening review — 2026-11-23

## LangGraph async API với PostgresSaver sync

- Triệu chứng: `ainvoke`/`astream_events` gọi `aget_tuple` chưa được triển khai.
- Nguyên nhân: graph async được compile bằng `PostgresSaver` đồng bộ.
- Khắc phục: khởi tạo `AsyncPostgresSaver` trong FastAPI lifespan, giữ async context đến shutdown, và dùng `aget_state` ở API.

## Approval không bảo vệ biên tool

- Triệu chứng: planner/HITL có thể duyệt step nhưng MCP tool vẫn bị gọi trực tiếp hoặc một approval mở khóa nhiều step.
- Khắc phục: lưu `approved_step_ids`, truyền capability qua A2A metadata/ContextVar và kiểm tra allowlist + approval tại mọi MCP invocation.

## Thread ID do client cung cấp

- Triệu chứng: hai principal có thể dùng cùng `thread_id` để đọc/ghi chung checkpoint.
- Khắc phục: atomic ownership registry và HMAC-scoped internal checkpoint ID; approval resolve owner từ registry và xác thực đúng pending step.

## CDC path traversal và stale index

- Triệu chứng: `file_path` có thể thoát `data/raw`; `_load_storage()` không index nội dung mới.
- Khắc phục: `resolve/relative_to`, hỗ trợ delete, rebuild persisted index trong worker thread rồi reset retriever singleton.

## Release attestation tự-tham-chiếu SHA

- Triệu chứng: CI yêu cầu `evidence/signoff/release-board.json.commit_sha == github.sha` trong khi chính tệp biên bản phải được commit vào SHA đó; không thể tạo artifact trung thực thỏa điều kiện.
- Nguyên nhân: một Git commit không thể chứa trước SHA của chính nó; đồng thời job publish từng đọc PRR tracked trong checkout thay vì artifact vừa sinh ở workflow hiện tại.
- Khắc phục: dùng giao thức hai commit. Hội đồng ký source commit `C`; commit `A` chỉ chứa biên bản có `commit_sha=C`; tag trỏ vào `A`. CI tải `prr-evidence-<SHA-A>`, xác minh `parent(A)=C` và diff `C..A` chỉ có `evidence/signoff/release-board.json` trước mọi thao tác push.

## Unit test ghi vào SQLite tracked

- Triệu chứng: chạy full suite làm thay đổi `data/memory/long_term_store.db` dù source không đổi.
- Nguyên nhân: một test khởi tạo `LongTermStore(force_sqlite=True)` mà không truyền `storage_dir`, nên dùng đường dẫn project mặc định.
- Khắc phục: mọi unit test SQLite phải dùng `tmp_path`; regression AST quét toàn bộ `tests/unit/test_*.py` và từ chối `force_sqlite=True` thiếu `storage_dir`.

## Circuit breaker test phụ thuộc tốc độ runner

- Triệu chứng: assertion trạng thái `OPEN` thỉnh thoảng thấy `HALF_OPEN` khi chạy xdist, đặc biệt lúc logging chậm.
- Nguyên nhân: test đặt recovery window 100 ms rồi sleep; độ trễ scheduler/logging có thể vượt cửa sổ trước assertion.
- Khắc phục: dùng recovery window 60 giây và dịch `last_failure_time` vượt ngưỡng để kiểm tra transition, không sleep và không patch đồng hồ toàn cục.

## BuildKit không nội suy ARG trong COPY --from

- Triệu chứng: cả ba image fail với `invalid reference format` tại `COPY --from=ghcr.io/astral-sh/uv:${UV_VERSION}`.
- Nguyên nhân: `COPY --from` chỉ nhận tên stage hoặc image reference tĩnh; Dockerfile frontend không mở rộng build ARG tại vị trí này.
- Khắc phục: khai báo `FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv`, sau đó dùng `COPY --from=uv`; regression kiểm tra named stage tồn tại và cấm lại mẫu cũ.

## Pull request bỏ qua container build/Trivy

- Triệu chứng: lint/test/manifest của PR xanh nhưng job `build-and-scan` bị `SKIPPED`; lỗi Dockerfile chỉ xuất hiện sau khi merge/push `main`.
- Nguyên nhân: job có điều kiện `if: github.event_name == 'push'` dù workflow đã nhận sự kiện pull request.
- Khắc phục: bỏ điều kiện push-only và thêm regression bắt buộc Docker build + Trivy fail-closed trên PR; publish/CD vẫn giữ tag-only riêng biệt.

## Trivy chặn image do digest base đã cũ

- Triệu chứng: image build thành công nhưng Trivy báo hàng loạt HIGH/CRITICAL OS CVE, ví dụ `apt 2.6.1`, dù tag Python vẫn thuộc dòng 3.12/bookworm.
- Nguyên nhân: digest bất biến đang trỏ tới snapshot Debian 12.12 cũ; pin digest bảo đảm tái lập nhưng không tự nhận bản vá bảo mật.
- Khắc phục: tra OCI index hiện hành từ registry, cập nhật đồng bộ cả ba Dockerfile sang digest đã rà soát và thêm regression exact-reference. Không dùng `ignore-unfixed`, severity waiver hoặc hạ `exit-code`; CI phải chứng minh Trivy sạch theo policy hiện hữu.
