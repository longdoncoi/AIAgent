# REVIEW_ARCHITECTURE & PROTOCOLS.md — Trụ Cột 1: Kiến Trúc Phân Tầng & Ranh Giới Giao Thức

> **Tài liệu kiểm định chuyên biệt (Production Readiness Review - Pillar 01 / 14)**  
> **Đơn vị phụ trách**: Chief AI Architect & Lead Backend Engineer  
> **Tài liệu tham chiếu**: [REVIEW.md](../../.workflows/reviews/REVIEW.md) · [ARCHITECTURE.md](../../Architecture.md) · [AGENTS.md](../../AGENTS.md)

---

## 1. Mục Tiêu & Phạm Vi Kiểm Định

Trụ cột này đánh giá sự tuân thủ nghiêm ngặt các nguyên tắc thiết kế bất biến của hệ thống Multi-Agent:
1. **Dependency Rule**: Tầng trên chỉ gọi tầng dưới qua interface chuẩn, kernel `src/agentic/core/` tuyệt đối độc lập.
2. **Stateless Services**: Mọi container Agent Pods đều không lưu trạng thái phiên (session state) cục bộ trong tiến trình; toàn bộ state do LangGraph Supervisor điều phối và lưu trữ ngoại vi (PostgreSQL, Redis).
3. **Chuẩn hóa Giao thức A2A (Agent-to-Agent)**: Phân định ranh giới giữa Supervisor và các Sub-Agents (Chat, RAG, Coding) qua HTTP/gRPC A2A Protocol, có khai báo `AgentCard`.
4. **Chuẩn hóa Giao thức MCP (Model Context Protocol)**: Đóng gói công cụ thành FastMCP servers độc lập, phân quyền nghiêm ngặt theo Allowlist.
5. **Hiệu năng mạng & Non-blocking I/O**: Tách luồng xử lý CPU/Disk nặng của RAG bằng `asyncio.to_thread()`, tái sử dụng TCP socket qua singleton connection pooling.

---

## 2. Danh Mục Checklist Kiểm Tra Chi Tiết

| ID | Trạng Thái | Mức Độ | Tiêu Chí Kiểm Tra (Verification Criteria) | Tệp Mã Nguồn / Cấu Hình | Phương Pháp & Lệnh Kiểm Chứng |
| :---: | :---: | :---: | :--- | :--- | :--- |
| **1.1** | `[ ]` | **P0** | **Quy tắc phân tầng một chiều (Dependency Rule)**: Tầng trên chỉ gọi tầng dưới qua interface. Kernel `src/agentic/core/` tuyệt đối độc lập, không import bất kỳ module nào thuộc `api`, `orchestrator`, `agents`, `rag`, `tools`, `memory`. | [src/agentic/core/](../../src/agentic/core/) | Chạy kiểm tra tĩnh AST/Import:<br>`python -c "import src.agentic.core as c; print('Core imports OK')"` |
| **1.2** | `[ ]` | **P0** | **Phi trạng thái của Microservices (Stateless Services)**: Mọi Agent Pod (`api`, `chat`, `rag`, `coding`) không lưu state cục bộ trong biến toàn cục hay bộ nhớ tiến trình. Mọi trạng thái phiên đều được lưu ra Postgres/Redis ngoại vi. | `src/agentic/orchestrator/state.py` | Kiểm tra scale ngang 2+ replicas không bị lệch hoặc mất state giữa các request cùng `thread_id`. |
| **1.3** | `[ ]` | **P0** | **Chuẩn hóa Giao thức A2A (Agent-to-Agent)**: Giao tiếp giữa Supervisor và Sub-Agents qua HTTP/gRPC A2A Protocol đúng cổng (`9100`: Chat, `9101`: RAG, `9102`: Coding). `AgentCard` được khai báo đầy đủ danh mục capabilities. | [src/agentic/a2a/cards.py](../../src/agentic/a2a/cards.py)<br>[src/agentic/a2a/client.py](../../src/agentic/a2a/client.py) | Gửi A2A Ping:<br>`curl -f -X GET http://localhost:9100/a2a/card`<br>`curl -f -X GET http://localhost:9101/a2a/card`<br>`curl -f -X GET http://localhost:9102/a2a/card` |
| **1.4** | `[ ]` | **P0** | **Chuẩn hóa Giao thức MCP & Phân quyền Allowlist**: Công cụ FastMCP chạy độc lập trên các cổng (`9001`: code exec, `9002`: repo, `9003`: web search). Phân quyền Allowlist nghiêm ngặt tại `policies.yaml` (Sub-agent không thể gọi tool ngoài danh sách cấp phép). | [configs/policies.yaml](../../configs/policies.yaml)<br>[src/agentic/tools/mcp_client.py](../../src/agentic/tools/mcp_client.py) | Chạy integration test kiểm tra chặn tool trái phép:<br>`pytest tests/unit/test_production_hardening.py -k test_tool_allowlist_enforcement` |
| **1.5** | `[ ]` | **P1** | **Xử lý Bất đồng bộ Non-blocking (Async RAG Offloading)**: Các tác vụ nặng về CPU/Disk I/O của LlamaIndex, BM25, Cross-Encoder phải được bọc trong `asyncio.to_thread()`, không làm block event loop chính của FastAPI. | [src/agentic/rag/tools.py](../../src/agentic/rag/tools.py) | Kiểm tra mã nguồn: Hàm `search_policy` trong `tools.py` phải gọi `await asyncio.to_thread(...)`. |
| **1.6** | `[ ]` | **P1** | **Connection Pooling Tải Cao (A2A Client Pooling)**: Sử dụng singleton `httpx.AsyncClient` với cấu hình pool tối ưu (`max_keepalive_connections=50`, `max_connections=200`, `keepalive_expiry=30.0`), triệt tiêu chi phí handshake TCP/SSL. | [src/agentic/a2a/client.py](../../src/agentic/a2a/client.py) | Kiểm tra lifecycle shutdown có gọi `await client.aclose()` để giải phóng socket sạch sẽ. |

---

## 3. Quy Trình Xác Minh & Lệnh Thực Thi (Pre-Flight Runbook)

### Bước 1: Kiểm thử AST/Imports của Core Kernel
```bash
python -c "
import sys, ast
from pathlib import Path
core_files = Path('src/agentic/core').glob('*.py')
for f in core_files:
    tree = ast.parse(f.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = getattr(node, 'module', '') or ''
            for alias in node.names:
                name = alias.name
                if any(x in mod or x in name for x in ['api', 'orchestrator', 'agents', 'tools', 'rag']):
                    print(f'VIOLATION in {f}: {mod}.{name}')
                    sys.exit(1)
print('AST Import Check: PASSED - Core is 100% independent.')
"
```

### Bước 2: Kiểm thử A2A Endpoints & Thẻ AgentCard
```bash
# Xác nhận các endpoint A2A phản hồi mã 200:
curl -s http://localhost:9100/a2a/card | jq .
curl -s http://localhost:9101/a2a/card | jq .
curl -s http://localhost:9102/a2a/card | jq .
```

### Bước 3: Chạy Unit Tests Trọng Tâm
```bash
pytest tests/unit/test_core.py tests/unit/test_production_hardening.py -v -k "test_tool_allowlist_enforcement or test_a2a_connection_pool"
```

### Bước 4: Kiểm Thử Luồng End-to-End API L1 & SSE Token Streaming
```bash
# 1. Kiểm tra Liveness & Readiness endpoints
curl -s -i http://localhost:8000/healthz | grep "200 OK"
curl -s -i http://localhost:8000/readyz | grep "200 OK"

# 2. Gửi truy vấn trò chuyện và kiểm tra luồng SSE Token Streaming
curl -N -X POST http://localhost:8000/v1/chat/stream \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <TOKEN_TEST>" \
  -d '{
    "message": "Tóm tắt ngắn gọn kiến trúc phân tầng của hệ thống",
    "thread_id": "dry_run_thread_001"
  }'
```

---

## 4. Danh Mục Giám Sát Sau Triển Khai (Post-Deployment Hypercare)

Sau khi hệ thống tiếp nhận lưu lượng truy cập thực tế:
- [ ] **T+1 Giờ**: Kiểm tra tỷ lệ lỗi ngắt kết nối bất thường của luồng SSE Stream (`Premature close` / `Broken pipe`) $< 0.01\%$.
- [ ] **T+1 Giờ**: Xác nhận 100% endpoints `/healthz` và `/readyz` phản hồi HTTP 200 OK dưới tải người dùng thực tế.
- [ ] **T+24 Giờ**: Đánh giá trạng thái socket và pool connection của A2A Client (`httpx.AsyncClient`), bảo đảm các kết nối được tái sử dụng hoàn toàn, không có hiện tượng rò rỉ socket connection (`ESTABLISHED` sockets duy trì ổn định trong pool).

---

## 5. Trạng Thái Nghiệm Thu & Liên Kết Ký Duyệt

- **Tổng số tiêu chí kiểm định**: 6 (P0: 4, P1: 2, P2: 0)
- **Đạt**: _____ / 6
- **Kết luận**: [ ] ĐẠT (PASS) &nbsp;&nbsp;&nbsp; [ ] CẦN KHẮC PHỤC (REJECT)

> 🎯 **Biên Bản Ký Duyệt**: Kết quả đánh giá của Trụ cột này được tổng hợp và ký duyệt chính thức tại [Bảng Chữ Ký Phê Chuẩn Của Hội Đồng Đánh Giá (Mục 6.3 - REVIEW.md)](../../.workflows/reviews/REVIEW.md#63-bảng-chữ-ký-phê-chuẩn-của-hội-đồng-đánh-giá).
