# .context — Ngữ Cảnh & Chỉ Dẫn Cốt Lõi Cho AI Coding Agent

> Thư mục này chứa các tài liệu định hình ngữ cảnh (Context), danh tính (Persona), các nguyên tắc bất biến (Design Inviolables) và quy chuẩn làm việc dành cho AI Coding Agent và kỹ sư trong dự án `agentic-platform`.

---

## 1. Bản Đồ Tài Liệu Ngữ Cảnh (Context Map)

| Tệp Ngữ Cảnh | Trọng Tâm | Đối Tượng Sử Dụng |
| :--- | :--- | :--- |
| [agent-instructions.md](file:///f:/PROJECTS/AIAgent/.context/agent-instructions.md) | Persona kỹ sư trưởng, 4 nguyên tắc bất biến, chu trình 5 bước tác chiến chuẩn | AI Coding Agent (Antigravity, Cursor, Claude Code, Cline) & Developer |

---

## 2. Hướng Dẫn Mở Rộng (Extension Guidelines)

Khi dự án mở rộng, các tài liệu ngữ cảnh bổ trợ có thể được thêm vào thư mục này theo các chuẩn sau:
- **`domain-glossary.md`**: Bảng tra cứu thuật ngữ chuyên ngành (Finance, Health, Telecom...) nếu dự án tích hợp thêm nghiệp vụ đặc thù.
- **`api-contracts.md`**: Bản đặc tả các giao ước API ngoại vi hoặc tích hợp bên thứ ba.
- **`team-conventions.md`**: Các quy ước nội bộ riêng của team phát triển (Branching model, Commit message format, PR template conventions).

---

## 3. Mối Liên Kết Với Hệ Sinh Thái

- **Điểm vào từ Root**: Được trỏ trực tiếp từ tiêu đề của [AGENTS.md](file:///f:/PROJECTS/AIAgent/AGENTS.md).
- **Trí nhớ thủ tục**: Liên kết tới các kịch bản thực thi tại [.skills/](file:///f:/PROJECTS/AIAgent/.skills/).
- **Trí nhớ ngữ cảnh**: Đồng bộ trạng thái với [.memory/](file:///f:/PROJECTS/AIAgent/.memory/).
- **Quy trình chuẩn**: Vận hành cùng [.workflows/](file:///f:/PROJECTS/AIAgent/.workflows/).
