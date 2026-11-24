# `.memory` — tri thức bền vững, nạp theo nhu cầu

Không đọc toàn bộ thư mục ở đầu mỗi task. Dùng bảng định tuyến sau:

| Tệp | Đọc khi | Chỉ cập nhật khi |
| --- | --- | --- |
| [`activeContext.md`](activeContext.md) | tiếp tục task, bàn giao, release, xem blocker | ưu tiên, blocker, next step hoặc baseline kiểm chứng đổi |
| [`architectureDecisions.md`](architectureDecisions.md) | thiết kế có đánh đổi hoặc xem lý do lịch sử | có quyết định kiến trúc bền vững mới |
| [`systemPatterns.md`](systemPatterns.md) | cần pattern triển khai đã chuẩn hóa | pattern đã xuất hiện trong code và test |
| [`techStack.md`](techStack.md) | cần runtime, thành phần hoặc port | topology/runtime đổi; không chép version từ lockfile |
| [`troubleshooting.md`](troubleshooting.md) | gặp triệu chứng tương tự | lỗi đã tái hiện và fix đã kiểm chứng |

## Kỷ luật token và độ tin cậy

- Mỗi fact chỉ có một nơi sở hữu; liên kết thay vì sao chép.
- Code, config, test, `pyproject.toml`, `uv.lock` và deployment manifests là nguồn sự thật cho runtime.
- `activeContext.md` là snapshot hiện tại, không phải changelog. Xóa hoặc thay fact cũ thay vì nối lịch sử vô hạn.
- Ghi ngày và lệnh/bằng chứng cho số liệu dễ lỗi thời. Không tuyên bố PASS nếu chưa chạy trong baseline được nêu.
- Giữ mục ngắn, tìm được bằng từ khóa và tránh diễn giải kiến thức phổ thông.
