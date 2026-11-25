# pr-preflight.md — Danh Sách Kiểm Tra Pre-Flight Trước Khi Tạo Pull Request

> **Bộ tiêu chí kiểm soát chất lượng nghiêm ngặt (Quality Gate)** trước khi đẩy code lên remote hoặc mở PR.  
> Kết nối với khung kiểm định: [.workflows/reviews/REVIEW_PRE-FLIGHT STAGE GATES.md](reviews/REVIEW_PRE-FLIGHT%20STAGE%20GATES.md).

---

## 1. Pre-Flight Checklist (10 Điểm Bắt Buộc)

- [ ] **1. Code Linting & Style**: Chạy `make lint` thành công không có lỗi từ Ruff hoặc Mypy.
- [ ] **2. Type Safety**: 100% hàm và biến mới có đầy đủ Type Annotations, không có `Any` lỏng lẻo.
- [ ] **3. Async Consistency**: Không có blocking call (sync I/O, `time.sleep`) trong async code.
- [ ] **4. Test Coverage**: Chạy `make test` đạt 100% test pass, coverage không bị giảm sút.
- [ ] **5. AI Quality Benchmark**: Nếu có sửa Prompt/RAG/Orchestrator, đã chạy `make eval-deep` và `make eval-ragas` đạt ngưỡng quy định.
- [ ] **6. Dependency Inviolables**: Không có import ngược giữa các tầng (L1 -> L2 -> L3 -> L4/L5 -> L6).
- [ ] **7. Security & Secrets**: Không commit file `.env`, token, API key, credential thực tế.
- [ ] **8. Schema Migration**: Nếu có thay đổi bảng cơ sở dữ liệu, đã tạo migration script với Alembic.
- [ ] **9. Memory Sync**: Đã cập nhật [.memory/activeContext.md](../.memory/activeContext.md) và [.memory/troubleshooting.md](../.memory/troubleshooting.md) (nếu có).
- [ ] **10. Clean Worktree**: Không để sót file tạm, cache file hoặc log thừa trong Git status.

---

## 2. Lệnh Kiểm Tra Nhanh

```bash
# Kiểm tra tổng thể trước khi commit
make lint
make test
make audit
```
