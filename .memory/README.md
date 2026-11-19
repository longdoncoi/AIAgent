# .memory — Hệ Thống Bộ Nhớ Ngữ Cảnh Bền Vững (Agent Persistent Memory)

> Thư mục này đóng vai trò bộ nhớ dài hạn (Episodic & Semantic Memory) của dự án. Mọi AI Coding Agent khi tham gia làm việc trên codebase đều phải đọc và cập nhật các tệp trong thư mục này để duy trì tính liên tục của ngữ cảnh.

---

## 1. Bản Đồ Bộ Nhớ (Memory Map)

| Tệp Bộ Nhớ | Mục Đích | Khi Nào Cần Đọc / Cập Nhật |
| :--- | :--- | :--- |
| [activeContext.md](file:///f:/PROJECTS/AIAgent/.memory/activeContext.md) | Trạng thái sprint, công việc đang làm dở, blocker, next steps | Đọc đầu mỗi phiên làm việc; Cập nhật cuối mỗi task |
| [architectureDecisions.md](file:///f:/PROJECTS/AIAgent/.memory/architectureDecisions.md) | Bản ghi các quyết định kiến trúc quan trọng (ADR) | Đọc khi thiết kế tính năng mới; Cập nhật khi đổi kiến trúc |
| [systemPatterns.md](file:///f:/PROJECTS/AIAgent/.memory/systemPatterns.md) | Các design patterns mẫu, quy tắc kiến trúc bất biến | Đọc khi bắt đầu viết code để tuân thủ pattern chuẩn |
| [troubleshooting.md](file:///f:/PROJECTS/AIAgent/.memory/troubleshooting.md) | Sổ tay ghi chép lỗi thực tế và cách giải quyết | Tra cứu khi gặp bug lạ; Cập nhật sau khi fix xong lỗi khó |
| [techStack.md](file:///f:/PROJECTS/AIAgent/.memory/techStack.md) | Chi tiết phiên bản thư viện, cấu hình cổng mạng, env | Tra cứu khi khởi tạo môi trường hoặc kiểm tra tương thích |

---

## 2. Giao Thức Đồng Bộ Bộ Nhớ (Memory Update Protocol)

- **Đầu mỗi phiên làm việc**:
  1. Đọc `activeContext.md` để xác định nhiệm vụ cần tiếp tục.
  2. Tra cứu `systemPatterns.md` để đảm bảo code sắp viết khớp với quy chuẩn dự án.
- **Trước khi hoàn tất phiên làm việc**:
  1. Cập nhật `activeContext.md` với các thay đổi vừa thực hiện và đề xuất bước tiếp theo.
  2. Nếu phát hiện một bẫy lỗi (gotcha) hoặc sửa một bug phức tạp, ghi lại vào `troubleshooting.md`.
