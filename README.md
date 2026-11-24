# Agentic Platform

Hierarchical multi-agent platform: FastAPI/SSE → LangGraph Supervisor → A2A Chat/RAG/Coding agents → MCP tools, LlamaIndex/Qdrant RAG, externalized memory and LiteLLM model routing.

## Quick start

Yêu cầu Python 3.12, `uv` và Docker.

```bash
cp .env.example .env
make install
make infra-up
```

Chạy từng nhóm trong terminal riêng:

```bash
make serve-mcp
make serve-agents
make dev
```

API docs: `http://localhost:8000/docs`.

## Quality gates

```bash
make lint
make test
make audit
make docs-check
```

Eval chuyên biệt: `make eval`, `make eval-deep`, `make eval-ragas`.

## Documentation map

- Coding-agent rules: [`AGENTS.md`](AGENTS.md)
- Architecture overview: [`Architecture.md`](Architecture.md)
- Current handoff/blockers: [`.memory/activeContext.md`](.memory/activeContext.md)
- Task runbooks: [`.skills/README.md`](.skills/README.md)
- Workflows/specifications/PRR: [`.workflows/README.md`](.workflows/README.md)

Chỉ đọc tài liệu khớp nhiệm vụ; `AGENTS.md` chứa router chi tiết.
