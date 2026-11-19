# architectureDecisions.md — Nhật Ký Quyết Định Kiến Trúc (ADR Log)

> Tệp này ghi lại các quyết định kỹ thuật cốt lõi (Architecture Decision Records) và lý do đằng sau các lựa chọn thiết kế của hệ thống.

---

## ADR-001: Lựa Chọn Mô Hình Hierarchical Multi-Agent Thay Vì Single-Agent Hoặc Swarm Phẳng
- **Bối cảnh**: Hệ thống xử lý các tác vụ phức tạp của doanh nghiệp bao gồm đàm thoại, tra cứu chính sách RAG và lập trình/thực thi mã nguồn sandbox.
- **Quyết định**: Triển khai mô hình phân cấp với L2 Supervisor (LangGraph) làm tổng chỉ huy, điều phối các L3 Sub-Agents chuyên biệt (Chat, RAG, Coding) qua A2A protocol.
- **Hệ quả**: Đảm bảo tính kiểm soát, khả năng phản tỉnh (reflection), quản lý checkpoint tập trung và kích hoạt Human-in-the-Loop (HITL) rõ ràng.

---

## ADR-002: Chuẩn Hóa Công Cụ Qua Model Context Protocol (MCP) Thay Vì Custom Function Calling
- **Bối cảnh**: Cần cung cấp các công cụ thực thi mã nguồn, truy cập git repo và tìm kiếm ngoài sandbox cho Agent.
- **Quyết định**: Sử dụng FastMCP để chuẩn hóa giao tiếp tool over HTTP/JSON-RPC.
- **Hệ quả**: Phân lập tool vào các tiến trình độc lập (cổng 9001, 9002, 9003), dễ dàng áp dụng Allowlist và thiết lập ranh giới bảo mật cho sandbox.

---

## ADR-003: Kiến Trúc Bộ Nhớ L6 & Checkpointer Đa Tầng
- **Bối cảnh**: Phiên làm việc của Agent cần có khả năng pause/resume (cho HITL) và khôi phục khi có sự cố.
- **Quyết định**: Kết hợp PostgresSaver và RedisSaver (có xác thực HMAC-SHA256) cho Short-term checkpointer, kết hợp cơ sở dữ liệu quan hệ + Vector DB cho Long-term memory. Quản lý schema bằng Alembic.
- **Hệ quả**: State luôn bền vững, có thể replay từng bước của DAG và tương thích với PgBouncer.

---

## ADR-004: Kiến Trúc Workflow, Skills và Memory Dành Cho AI Coding Agents
- **Bối cảnh**: Cần một môi trường làm việc chuẩn hóa cho các AI Coding Agent tham gia phát triển dự án, tránh mất ngữ cảnh giữa các phiên làm việc.
- **Quyết định**: Thiết lập hệ thống thư mục `.workflows/`, `.skills/`, `.memory/` và chỉ dẫn tác chiến `.context/agent-instructions.md`.
- **Hệ quả**: Bất kỳ AI Agent hay developer nào mở repo đều nắm được ngay persona, quy tắc bất biến, quy trình kiểm thử và ngữ cảnh dự án mà không cần giải thích lại.
