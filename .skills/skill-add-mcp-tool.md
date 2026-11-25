# skill-add-mcp-tool.md — Quy Trình Thêm Mới MCP Tool (L4)

> **Mục tiêu**: Hướng dẫn xây dựng và đăng ký một công cụ theo chuẩn Model Context Protocol (MCP) sử dụng FastMCP.

---

## 4 Bước Triển Khai Chuẩn

### Bước 1: Khởi Tạo Server FastMCP
Tạo file trong `src/agentic/tools/servers/<tool_name>_server.py`:
```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("my_tool_server")

@mcp.tool()
async def execute_task(param_1: str, param_2: int = 10) -> str:
    """Mô tả chi tiết chức năng của tool cho LLM hiểu."""
    try:
        # Xử lý logic an toàn
        return "Kết quả xử lý"
    except Exception as exc:
        return f"Lỗi thực thi: {exc}"

if __name__ == "__main__":
    mcp.run()
```

### Bước 2: Khai Báo Chính Sách & Risk Level
Thêm cấu hình vào `configs/policies.yaml`:
```yaml
tools:
  execute_task:
    risk_level: low  # low | medium | high
    timeout_seconds: 30
    allowed_agents:
      - coding_agent
```
> **Lưu ý**: Nếu tool có khả năng ghi đè file, xoá dữ liệu hoặc chạy lệnh shell, bắt buộc đặt `risk_level: high` để kích hoạt Human-in-the-Loop (HITL).

### Bước 3: Đăng Ký Client & Port
1. Gán cổng cho tool server trong [Makefile](file:///f:/PROJECTS/AIAgent/Makefile) (target `serve-mcp`).
2. Cập nhật `src/agentic/tools/mcp_client.py` để Sub-Agent tự động kết nối và nạp tool khi khởi động.

### Bước 4: Viết Unit Test
Tạo `tests/unit/test_<tool_name>.py` kiểm tra schema của tool, khả năng xử lý input hợp lệ và xử lý ngoại lệ khi lỗi.
