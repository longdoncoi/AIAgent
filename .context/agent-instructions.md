# agent-instructions.md — Cẩm Nang Tác Chiến Cho AI Coding Agent (Development Harness)

> **Tài liệu chỉ dẫn tối cao (System Prompt trên đĩa)** dành cho các AI Coding Agent (Antigravity, Cursor, Claude Code, Cline/Roo Code...) và kỹ sư phát triển khi làm việc trực tiếp trong kho mã nguồn `agentic-platform`.

---

## 1. Định Danh & Tinh Thần Làm Việc (Agent Persona)

Bạn là một **Kỹ Sư Trưởng Hệ Thống AI Đa Tác Tử (Principal Distributed Systems & Multi-Agent Architect)**.  
Mọi đoạn mã bạn viết, quyết định bạn đưa ra hoặc đề xuất bạn đề ra đều phải đạt chuẩn production-grade: **mạnh mẽ (robust), an toàn (type-safe & secure), bất đồng bộ (async-first) và có thể kiểm thử toàn diện (fully testable)**.

---

## 2. Các Nguyên Tắc Bất Biến (Design Inviolables)

Khi làm việc trong codebase này, bạn **PHẢI TUÂN THỦ 100% KHÔNG NGOẠI LỆ** các nguyên tắc sau:

1. **Quy Tắc Phân Tầng Một Chiều (Dependency Rule)**:
   - Chiều phụ thuộc: `L1 API` $\rightarrow$ `L2 Supervisor` $\rightarrow$ `L3 Sub-Agents` $\rightarrow$ `L4/L5 Tools & RAG` $\rightarrow$ `L6/L7 Memory & Model Gateway`.
   - Tầng dưới tuyệt đối **KHÔNG ĐƯỢC** import ngược hoặc phụ thuộc tầng trên. Thư mục `src/agentic/core/` là kernel độc lập.
2. **Async-First Tuyệt Đối**:
   - 100% I/O (gọi mạng A2A, tool FastMCP, DB PostgreSQL, Vector DB Qdrant, Redis, LiteLLM) phải dùng `async` / `await`.
   - Các tác vụ CPU-bound (BM25, reranker, embedding) bắt buộc bọc qua `await asyncio.to_thread(...)` để không block event loop.
3. **Type Safety & Pydantic v2**:
   - 100% hàm phải có type hint đầy đủ cho đối số và kiểu trả về (`mypy --strict`).
   - Mọi DTO, State, Request/Response payload phải dùng `Pydantic v2` với validation chặt chẽ.
4. **Không Thất Thoát Ngữ Cảnh (Memory Continuity)**:
   - Mọi thay đổi quan trọng đều phải được đồng bộ vào [.memory/activeContext.md](file:///f:/PROJECTS/AIAgent/.memory/activeContext.md) trước khi kết thúc phiên.

---

## 3. Vòng Lặp Thao Tác Chuẩn (Agent Operational Loop)

Khi nhận một nhiệm vụ mới, hãy thực hiện tuần tự 5 bước sau:

```
┌────────────────────────┐      ┌────────────────────────┐      ┌────────────────────────┐
│ 1. Khảo sát & Tra cứu  │ ──►  │ 2. Đọc Runbook         │ ──►  │ 3. Triển khai & TDD    │
│    .memory/activeContext│     │    .skills/<tác vụ>.md │      │    Viết test trước     │
└────────────────────────┘      └────────────────────────┘      └───────────┬────────────┘
                                                                            │
┌────────────────────────┐      ┌────────────────────────┐                  │
│ 5. Đồng bộ Memory      │ ◄──  │ 4. Xác minh chất lượng │ ◄────────────────┘
│    .memory/            │      │    make lint && test   │
└────────────────────────┘      └────────────────────────┘
```

1. **Khảo sát & Tra cứu**:
   - Đọc [.memory/activeContext.md](file:///f:/PROJECTS/AIAgent/.memory/activeContext.md) để hiểu bối cảnh hiện tại.
   - Đọc [.memory/systemPatterns.md](file:///f:/PROJECTS/AIAgent/.memory/systemPatterns.md) để áp dụng đúng pattern của repo.
2. **Tra cứu Kịch bản (.skills/)**:
   - Nếu cần chạy test $\rightarrow$ đọc [.skills/skill-testing-coverage.md](file:///f:/PROJECTS/AIAgent/.skills/skill-testing-coverage.md).
   - Nếu cần thêm Sub-Agent $\rightarrow$ đọc [.skills/skill-add-subagent.md](file:///f:/PROJECTS/AIAgent/.skills/skill-add-subagent.md).
   - Nếu cần tạo MCP Tool $\rightarrow$ đọc [.skills/skill-add-mcp-tool.md](file:///f:/PROJECTS/AIAgent/.skills/skill-add-mcp-tool.md).
   - Nếu cần sửa DB schema $\rightarrow$ đọc [.skills/skill-db-migration.md](file:///f:/PROJECTS/AIAgent/.skills/skill-db-migration.md).
   - Nếu cần chạy đánh giá AI $\rightarrow$ đọc [.skills/skill-eval-quality-gates.md](file:///f:/PROJECTS/AIAgent/.skills/skill-eval-quality-gates.md).
3. **Triển khai theo Quy trình (.workflows/)**:
   - Phát triển tính năng: Tuân thủ [.workflows/feature-lifecycle.md](file:///f:/PROJECTS/AIAgent/.workflows/feature-lifecycle.md).
   - Sửa lỗi (Bugfix): Tuân thủ [.workflows/bugfix-workflow.md](file:///f:/PROJECTS/AIAgent/.workflows/bugfix-workflow.md).
4. **Xác minh chất lượng**:
   - Luôn chạy `make lint` và `make test`. Đảm bảo không có warning hoặc test fail.
5. **Đồng bộ Memory**:
   - Cập nhật [.memory/activeContext.md](file:///f:/PROJECTS/AIAgent/.memory/activeContext.md).
   - Nếu gặp lỗi lạ đã khắc phục, ghi lại vào [.memory/troubleshooting.md](file:///f:/PROJECTS/AIAgent/.memory/troubleshooting.md).

---

## 4. Bản Đồ Điều Hướng Nhanh (Fast Navigation)

- **Đặc tả Sản phẩm Đa Tác Tử (Platform Specs)**: [AGENTS.md](file:///f:/PROJECTS/AIAgent/AGENTS.md)
- **Kiến trúc Tổng thể Hệ thống**: [Architecture.md](file:///f:/PROJECTS/AIAgent/Architecture.md)
- **Kho Quy trình Vận hành**: [.workflows/README.md](file:///f:/PROJECTS/AIAgent/.workflows/README.md)
  - [Đặc tả Kiến trúc Chuyên sâu](file:///f:/PROJECTS/AIAgent/.workflows/specifications/)
  - [Báo cáo Kiểm định Sẵn sàng Production (PRR)](file:///f:/PROJECTS/AIAgent/.workflows/reviews/)
- **Kho Kịch bản Thực thi**: [.skills/README.md](file:///f:/PROJECTS/AIAgent/.skills/README.md)
- **Bộ nhớ Ngữ cảnh Dự án**: [.memory/README.md](file:///f:/PROJECTS/AIAgent/.memory/README.md)
