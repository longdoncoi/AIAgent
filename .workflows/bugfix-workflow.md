# Bugfix workflow

1. **Tái hiện**: thu input/log/trace tối thiểu và tạo failing test khi khả thi. Xác nhận test fail đúng triệu chứng trước khi sửa.
2. **Khoanh vùng**: lần theo boundary và state; phân biệt root cause với exception cuối. Kiểm tra async blocking, schema drift, tenant/capability scope, cache key và lifecycle resource.
3. **Sửa tối thiểu**: giữ API compatibility và không che lỗi bằng fallback làm yếu security/durability.
4. **Chứng minh**: test tái hiện phải pass; thêm negative test cho failure mode; chạy regression theo blast radius.
5. **Ghi tri thức**: chỉ thêm vào `.memory/troubleshooting.md` nếu gotcha có khả năng lặp lại và fix đã được kiểm chứng. Cập nhật `activeContext.md` chỉ khi blocker/next step đổi.

Tuân thủ [`coding-workflow.md`](coding-workflow.md); dùng skill test hoặc quality phù hợp từ [`.skills`](../.skills/README.md).
