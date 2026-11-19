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
