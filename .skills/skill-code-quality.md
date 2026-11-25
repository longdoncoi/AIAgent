# skill-code-quality.md — Quy Chuẩn Kiểm Tra Mã Nguồn (Linting & Static Analysis)

> **Mục tiêu**: Hướng dẫn thực hiện kiểm tra tĩnh chất lượng code, type check và bảo mật trước khi commit.

---

## 1. Lệnh Kiểm Tra Nhanh

```bash
# 1. Chạy toàn bộ linting (Ruff kiểm tra style + Mypy kiểm tra kiểu dữ liệu)
make lint

# 2. Tự động sửa các lỗi format đơn giản bằng Ruff
ruff format src/ tests/
ruff check --fix src/ tests/

# 3. Kiểm tra riêng Mypy với strict mode
mypy src/

# 4. Kiểm tra lỗ hổng bảo mật các thư viện phụ thuộc
make audit
# tương đương: pip-audit
```

---

## 2. Quy Tắc Viết Code Bất Biến

1. **Type Annotations**:
   - Mọi hàm công khai (public function) và nội bộ (internal helper) đều phải có type hint cho từng đối số và return value.
   - Tránh dùng `Any`. Nếu dữ liệu chưa xác định, dùng `TypeVar` hoặc `object` kèm type narrowing (`isinstance`).
2. **Pydantic v2**:
   - Sử dụng `from pydantic import BaseModel, Field, ConfigDict`.
   - Cấu hình `model_config = ConfigDict(strict=True, extra="forbid")` cho các DTO nhạy cảm để chống injection các field lạ.
3. **Async / Non-blocking**:
   - Tuyệt đối không dùng `requests`, `time.sleep()`, hoặc đồng bộ file read trong async functions.
   - Sử dụng `httpx.AsyncClient`, `asyncio.sleep()`, và `aiofiles` hoặc `asyncio.to_thread()`.
