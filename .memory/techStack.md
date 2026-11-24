# Tech stack và topology

Không chép version package vào đây. Dùng `pyproject.toml` cho constraint, `uv.lock` cho version chính xác, `.env.example`/`src/agentic/core/config.py` cho setting và deployment manifests cho production topology.

## Thành phần

| Lớp | Công nghệ / vị trí |
| --- | --- |
| API | FastAPI, Uvicorn, SSE — `src/agentic/api/` |
| Orchestration | LangGraph — `src/agentic/orchestrator/` |
| A2A agents | Chat, RAG, Coding — `src/agentic/agents/` |
| Tools | MCP/FastMCP — `src/agentic/tools/` |
| Knowledge | LlamaIndex, Qdrant, hybrid retrieval — `src/agentic/rag/` |
| State | PostgreSQL, Redis, Alembic — `src/agentic/memory/`, `migrations/` |
| Models | LiteLLM → vLLM / llama.cpp — `src/agentic/llm/`, `deploy/serving/` |
| Quality | Pytest, Ruff, Mypy, DeepEval/RAGAS — `tests/`, `evals/` |
| Observability | OpenTelemetry, Prometheus, LangSmith — `src/agentic/observability/` |

Runtime chuẩn: Python 3.12, package manager `uv`.

## Port mặc định local

| Dịch vụ | Port |
| --- | ---: |
| API / LiteLLM / vLLM | `8000` / `4000` / `8001` |
| llama.cpp chat / embedding | `8080` / `8081` |
| MCP code / repo / web | `9001` / `9002` / `9003` |
| A2A chat / RAG / coding | `9100` / `9101` / `9102` |
| PostgreSQL / Qdrant / Redis / Temporal | `5432` / `6333` / `6379` / `7233` |

Xác minh port trước khi sửa bằng `Makefile`, `src/agentic/core/config.py` và `deploy/`; bảng này chỉ là bản đồ tra cứu.
