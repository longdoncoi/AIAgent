# feature-lifecycle.md — Vòng Đời Phát Triển Tính Năng Mới

> **Quy trình chuẩn hóa 5 bước** dành cho kỹ sư và AI Coding Agent khi phát triển tính năng mới trong `agentic-platform`.  
> Tham chiếu: [coding-workflow.md](coding-workflow.md) · [.memory/activeContext.md](../.memory/activeContext.md).

---

## Sơ Đồ Vòng Đời Tính Năng (5-Stage Flow)

```
[1. Khảo Sát & Context] ──► [2. Thiết Kế Schema/Spec] ──► [3. Test-First (TDD)]
                                                                    │
[5. Sync Memory & PR]  ◄── [4. Implement & Verify]   ◄──────────────┘
```

---

## Chi Tiết Các Bước Thực Hiện

### Bước 1: Khảo Sát & Xác Lập Ngữ Cảnh
- Đọc [.memory/activeContext.md](../.memory/activeContext.md) để nắm bắt sprint và các task đang thực hiện.
- Tra cứu [.memory/systemPatterns.md](../.memory/systemPatterns.md) và [Architecture.md](../Architecture.md) để xác định rõ tính năng thuộc tầng kiến trúc nào (L1 - L9).
- Nếu tính năng yêu cầu thay đổi kiến trúc lớn, thảo luận hoặc lập ADR mới trong [.memory/architectureDecisions.md](../.memory/architectureDecisions.md).

### Bước 2: Thiết Kế Schema, DTO & Giao Diện Trước
- Định nghĩa rõ Data Models / State Schema bằng `Pydantic v2` (kèm validation logic).
- Nếu là Sub-Agent mới: Định nghĩa `AgentCard` và input/output payload.
- Nếu là MCP Tool mới: Định nghĩa parameters và risk level trong `configs/policies.yaml`.

### Bước 3: Viết Kiểm Thử Trước (TDD / Test Scaffolding)
- Viết unit test tương ứng trong `tests/unit/test_<feature>.py`.
- Tạo mock dữ liệu cho LLM calls, Network RPC hoặc Database query.
- Chạy `pytest tests/unit/test_<feature>.py` để xác nhận test FAIL (Red phase).

### Bước 4: Triển Khai Mã Nguồn & Xác Thực Chất Lượng
- Viết mã nguồn hoàn chỉnh tuân thủ nguyên tắc Async-first và Type-safe.
- Chạy kiểm tra:
  ```bash
  make lint             # ruff + mypy
  pytest tests/unit/test_<feature>.py -v
  make test             # Chạy toàn bộ test suite để đảm bảo không hồi quy
  ```
- Nếu tính năng liên quan đến Prompt/RAG/Reasoning, chạy:
  ```bash
  make eval-deep        # DeepEval
  make eval-ragas       # RAGAS benchmark
  ```

### Bước 5: Cập Nhật Bộ Nhớ Ngữ Cảnh & Chuẩn Bị PR
- Cập nhật [.memory/activeContext.md](../.memory/activeContext.md): Đánh dấu task đã hoàn thành, ghi lại các thay đổi quan trọng và bước tiếp theo.
- Nếu phát hiện bẫy lỗi (gotcha) trong quá trình code, ghi lại vào [.memory/troubleshooting.md](../.memory/troubleshooting.md).
- Thực hiện kiểm tra pre-flight theo [pr-preflight.md](pr-preflight.md).
