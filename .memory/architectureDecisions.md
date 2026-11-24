# Architecture Decision Log

Chỉ ghi quyết định bền vững và đánh đổi. Implementation chi tiết thuộc `ARCHITECTURE.md` hoặc specification tương ứng.

## ADR-001 — Hierarchical multi-agent

- **Quyết định**: L2 LangGraph Supervisor điều phối các L3 Chat/RAG/Coding agents qua A2A.
- **Lý do**: cần checkpoint, reflection, fan-out và HITL tập trung thay vì swarm phẳng khó kiểm soát.
- **Hệ quả**: sub-agent stateless; orchestration state thuộc Supervisor và external stores.

## ADR-002 — MCP cho biên công cụ

- **Quyết định**: chuẩn hóa tool bằng MCP/FastMCP thay vì function-calling riêng từng agent.
- **Lý do**: cần process isolation, schema thống nhất, allowlist và policy enforcement tại biên.
- **Hệ quả**: tool server độc lập; high-risk call cần capability/HITL hợp lệ.

## ADR-003 — Externalized, resumable state

- **Quyết định**: dùng async Postgres checkpointer; Redis payload có HMAC; long-term memory dùng relational/vector stores và Alembic.
- **Lý do**: hỗ trợ pause/resume/replay và scale ngang an toàn.
- **Hệ quả**: service không sở hữu session state; migration và tenant scope là contract production.

## ADR-004 — Repo-local agent guidance

- **Quyết định**: tách chỉ dẫn, memory, skill và workflow thành `.context/`, `.memory/`, `.skills/`, `.workflows/`.
- **Lý do**: giữ kiến thức dự án gần code và dùng được qua nhiều coding agent.
- **Hệ quả**: tài liệu phải có owner rõ và không trùng lặp.

## ADR-005 — Progressive context disclosure

- **Quyết định**: `AGENTS.md` chỉ chứa invariant + router; skill dùng `<name>/SKILL.md`; tài liệu kiến trúc/review chỉ nạp theo domain.
- **Lý do**: giảm token khởi tạo, tránh số liệu cũ lấn át bằng chứng runtime và giảm chi phí bảo trì.
- **Hệ quả**: không preload toàn bộ docs; link tương đối; `make docs-check` bảo vệ cấu trúc và link.
