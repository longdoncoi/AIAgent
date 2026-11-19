# Chính Sách Kỹ Thuật & Vận Hành Đa Tác Tử (Enterprise Multi-Agent Platform Policy)

## 1. Quy Định Thực Thi Mã Nguồn & Sandbox (Code Execution & Sandbox Policy)
- Tất cả các tác vụ thực thi mã nguồn tự động của `CodingAgent` bắt buộc phải chạy bên trong container sandbox cô lập (gVisor hoặc Docker non-privileged).
- Giới hạn thời gian (timeout) tối đa cho mỗi tiến trình chạy thử nghiệm là 30 giây.
- Nghiêm cấm các lệnh truy cập tài nguyên mạng bên ngoài sandbox nếu không có cờ cấp phép tường lửa.
- Thao tác ghi đè tệp mã nguồn dự án (`repo_write_file`) hoặc thực thi lệnh shell hệ thống (`code_exec_bash`) luôn được phân loại mức độ rủi ro cao (`risk_level: high`) và bắt buộc phải tạm dừng chờ phê duyệt từ người dùng (Human-In-The-Loop - HITL).

## 2. Bảo Vệ Dữ Liệu Cá Nhân & Thông Tin Nhạy Cảm (Data Privacy & PII Policy)
- Mọi dữ liệu đầu vào chứa địa chỉ Email, Số điện thoại, Mã định danh cá nhân (CCCD/CMND), Số thẻ tín dụng, hoặc API Secret Token (`sk-...`, `ghp_...`, Bearer tokens) phải được phát hiện và ẩn danh hoá (redact) trước khi lưu vào lịch sử hội thoại hoặc gửi qua các LLM bên ngoài.
- Dữ liệu tri thức nội bộ truy xuất từ hệ thống RAG không được phép hiển thị ra ngoài nếu chưa qua bước kiểm định mức độ nhạy cảm của Guardrail Output Filter.

## 3. Kiến Trúc Phân Tầng & Giao Thức Kết Nối (Architectural Standards)
- **A2A Protocol**: Mọi giao tiếp giữa Supervisor và Sub-Agents bắt buộc phải tuân thủ chuẩn Agent-to-Agent protocol thông qua schema `A2ATaskRequest` và `AgentCard`.
- **MCP Protocol**: Các công cụ (Tools) phải được đóng gói theo chuẩn Model Context Protocol (FastMCP / MCPServer) và chịu sự kiểm soát của Allowlist phân quyền theo từng tác tử.
- **Stateless Sub-Agents**: Sub-Agents không được tự ý lưu trữ trạng thái phiên làm việc cục bộ. Mọi trạng thái và lịch sử phiên phải do LangGraph Supervisor quản lý thông qua cơ chế Checkpointing.
