# skill-add-subagent.md — Quy Trình Thêm Mới Sub-Agent (L3)

> **Mục tiêu**: Hướng dẫn 6 bước chuẩn hóa để tạo mới một Sub-Agent hoạt động độc lập và giao tiếp qua chuẩn A2A (Agent-to-Agent).

---

## 6 Bước Thực Thi Chuẩn

### Bước 1: Khởi Tạo Thư Mục Agent
Tạo thư mục con tại `src/agentic/agents/<agent_name>/`:
- `__init__.py`
- `graph.py`: Xây dựng LangGraph ReAct loop.
- `server.py`: Service bọc bằng `A2A AgentExecutor`.

### Bước 2: Xây Dựng Graph Kế Thừa Base
Trong `graph.py`, kế thừa từ hàm chuẩn `build_react_graph()` tại `src/agentic/agents/base.py`:
```python
from src.agentic.agents.base import build_react_graph

def get_agent_graph():
    return build_react_graph(
        agent_name="my_new_agent",
        tools=[...],  # Các MCP Tools được cấp quyền
        system_prompt="...",
    )
```

### Bước 3: Định Nghĩa AgentCard
Trong `src/agentic/a2a/cards.py`, khai báo metadata cho Agent mới:
```python
AgentCard(
    name="my_new_agent",
    description="Mô tả chức năng chuyên biệt của agent...",
    skills=["skill_1", "skill_2"],
    endpoint="http://localhost:9103/a2a",
)
```

### Bước 4: Khởi Tạo Server A2A
Trong `server.py`, cấp phát port tiếp theo (ví dụ `9103`):
```python
import uvicorn
from src.agentic.a2a.executor import create_a2a_app

app = create_a2a_app(get_agent_graph())

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9103)
```

### Bước 5: Cập Nhật Supervisor Dispatch
1. Cập nhật prompt điều phối tại `configs/prompts/planner.md`.
2. Khai báo endpoint vào bảng tra cứu trong `src/agentic/orchestrator/nodes/dispatch.py`.
3. Bổ sung script khởi động vào [Makefile](file:///f:/PROJECTS/AIAgent/Makefile) tại target `serve-agents`.

### Bước 6: Viết Integration Test
Tạo file `tests/integration/test_<agent_name>.py` để kiểm tra khả năng nhận nhiệm vụ qua A2A và trả về output hợp lệ.
