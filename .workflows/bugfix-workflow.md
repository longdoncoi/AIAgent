# bugfix-workflow.md — Quy Trình Tái Hiện & Vá Lỗi Chuẩn

> **Quy trình chuẩn hóa 5 bước** dành cho việc điều tra, tái hiện và khắc phục sự cố trong `agentic-platform`.  
> Tham chiếu: [coding-workflow.md](coding-workflow.md) · [.memory/troubleshooting.md](../.memory/troubleshooting.md).

---

## Sơ Đồ Quy Trình Vá Lỗi

```
[1. Tái Hiện Lỗi (Failing Test)] ──► [2. Định Vị Nguyên Nhân Gốc] ──► [3. Bản Vá Tối Thiểu (Minimal Fix)]
                                                                               │
[5. Ghi Chép Vào Troubleshooting] ◄── [4. Kiểm Thử Hồi Quy (make test)] ◄──────┘
```

---

## Chi Tiết Các Bước

### Bước 1: Viết Test Tái Hiện Lỗi (Reproduce)
- Tuyệt đối không sửa mã nguồn trước khi có test tái hiện được bug.
- Viết test case cô lập lỗi trong `tests/unit/` hoặc `tests/integration/` phản ánh đúng input gây crash/lỗi.
- Chạy test và xác nhận lỗi xuất hiện chính xác như mô tả.

### Bước 2: Định Vị Nguyên Nhân Gốc (Root Cause Analysis)
- Đọc log truy vết (OpenTelemetry/Jaeger/LangSmith) nếu lỗi xuất hiện trong quá trình inference hoặc A2A dispatch.
- Kiểm tra các nguyên nhân thường gặp:
  - Vi phạm Async (ví dụ gọi hàm sync I/O trong async event loop).
  - Lệch schema Pydantic giữa các phiên bản checkpoint hoặc payload A2A.
  - Race condition trong Connection Pool (Postgres, Redis).
  - Lỗi deserialization state hoặc checkpoint HMAC mismatch.

### Bước 3: Triển Khai Bản Vá Tối Thiểu (Minimal & Precise Fix)
- Sửa lỗi tập trung tại đúng điểm phát sinh, không thay đổi lan man các file không liên quan.
- Giữ nguyên giao diện API và tính tương thích ngược (backward compatibility).

### Bước 4: Kiểm Thử Hồi Quy Toàn Diện
- Chạy test tái hiện: Đảm bảo test đã PASS (Green phase).
- Chạy toàn bộ test suite để đảm bảo không phát sinh lỗi phụ:
  ```bash
  make lint
  make test
  ```

### Bước 5: Ghi Chép Vào Bộ Nhớ (Knowledge Retention)
- Bổ sung trường hợp lỗi, nguyên nhân gốc và giải pháp vào [.memory/troubleshooting.md](../.memory/troubleshooting.md).
- Cập nhật [.memory/activeContext.md](../.memory/activeContext.md).
