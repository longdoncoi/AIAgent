# `.context` — lớp chỉ dẫn ngắn

Mục tiêu của thư mục này là định tuyến, không sao chép kiến trúc hay runbook.

- [`../AGENTS.md`](../AGENTS.md): hợp đồng bắt buộc và bản đồ nạp ngữ cảnh; đây là điểm vào mặc định.
- [`agent-instructions.md`](agent-instructions.md): vòng thao tác thực tế và tiêu chí ghi memory.

Chi tiết chỉ được nạp theo yêu cầu từ [`.memory`](../.memory/README.md), [`.skills`](../.skills/README.md) và [`.workflows`](../.workflows/README.md). Khi thêm chỉ dẫn mới, đặt thông tin ở đúng một nơi và liên kết tới đó; không lặp lại nội dung giữa các lớp.
