# skill-testing-coverage.md — Hướng Dẫn Chạy Test & Đo Lường Coverage

> **Mục tiêu**: Hướng dẫn chạy kiểm thử đơn vị (unit tests), tích hợp (integration tests) và đo lường độ bao phủ mã nguồn theo đúng chuẩn của `agentic-platform`.

---

## 1. Lệnh Kiểm Thử Cơ Bản

```bash
# Chạy toàn bộ test suite kèm đo lường coverage
make test

# Chạy một file test cụ thể với output chi tiết
pytest tests/unit/test_coding_agent_workflow.py -v

# Chạy test theo từ khóa hoặc pattern
pytest tests/ -k "rag" -v

# Chạy và hiển thị log chi tiết (không bắt output)
pytest tests/ -v -s
```

---

## 2. Quy Tắc Mock Dữ Liệu Trong Tests

1. **LiteLLM / AI Gateway**:
   - Sử dụng fixture mock trong `tests/conftest.py`.
   - Tuyệt đối không gọi model thật trong Unit Test để tránh phát sinh chi phí và phụ thuộc mạng.
2. **Database & Cache (Postgres, Redis, Qdrant)**:
   - Sử dụng in-memory mock hoặc container tạm thời được dọn dẹp sau mỗi test session.
3. **A2A Network Calls**:
   - Sử dụng `respx` hoặc `unittest.mock.AsyncMock` để mock `httpx.AsyncClient`.

---

## 3. Cạm Bẫy Cần Tránh (Pitfalls)

- **Unclosed Async Loops**: Khi viết test async, luôn dùng `@pytest.mark.asyncio`.
- **Dangling State**: Đảm bảo dọn dẹp state checkpointer giữa các bài test để tránh test này ảnh hưởng test khác.
