# Coding workflow

## Trước khi sửa

1. Xác định contract ở code, schema, test và config; kiểm tra `git status --short`.
2. Tìm implementation bằng `rg`; đọc đúng specification/skill nếu chạm biên A2A, MCP, persistence, safety hoặc eval.
3. Nêu giả định chỉ khi chưa thể xác minh từ repo.

## Khi triển khai

- Giữ dependency một chiều và service stateless.
- Dùng async cho I/O; không chặn event loop bằng sync/CPU work.
- Giữ type strict và validate payload ở boundary bằng Pydantic v2.
- Fail closed cho auth, tenant, policy, capability, HITL và production transports.
- Tránh refactor ngoài phạm vi trừ khi bắt buộc để sửa đúng nguyên nhân.

## Khi xác minh

1. Chạy test/lint hẹp cho vòng lặp nhanh.
2. Chạy gate rộng theo blast radius: `make lint`, `make test`, eval hoặc audit khi phần tương ứng thay đổi.
3. Soát diff, generated files, link/docs và secret.
4. Ghi rõ lệnh đã chạy, kết quả và kiểm tra chưa chạy.

Lệnh canonical nằm trong `Makefile`; không sao chép danh sách lệnh vào workflow này.
