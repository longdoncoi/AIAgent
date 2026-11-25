# Agentic Platform

Hierarchical Multi-Agent AI Platform built with:
- **L1 Interface**: FastAPI with Server-Sent Events (SSE) streaming and Human-in-the-Loop (HITL) approval endpoints.
- **L2 Orchestration**: LangGraph Supervisor orchestrator implementing Planning, Parallel Dispatch (fan-out), ReAct loop, and Reflection.
- **L3 Sub-Agents**: ChatAgent, RAGAgent, and CodingAgent interacting over standard **A2A (Agent-to-Agent)** protocol.
- **L4 Tools**: FastMCP tool servers (`code_exec_server`, `repo_server`, `web_search_server`) with dynamic policy allowlist and risk evaluation.
- **L5 Knowledge (RAG)**: LlamaIndex with hierarchical node chunking (parent 1024, child 256 tokens), BM25 + dense hybrid search, reciprocal rank fusion (RRF), and cross-encoder reranking.
- **L6 Memory**: Short-term stateful checkpointing (PostgresSaver / MemorySaver) and long-term semantic, episodic, and procedural memory stores.
- **L7 Model Serving**: LiteLLM model gateway routing requests to vLLM (high-throughput GPU inference) or llama.cpp (CPU/edge GGUF and embeddings).
- **L8 Safety & L9 Observability**: Input/output guardrails (PII redaction, prompt injection defense), OpenTelemetry tracing, Prometheus metrics, and automated LLM-as-a-judge evals.

---

## Architecture Overview

```
Client (Web UI / CLI)
        │
        ▼
L1 Interface (FastAPI / SSE streaming / Auth / Approvals)
        │
        ▼
L2 Supervisor (LangGraph: Plan -> Dispatch -> ReAct -> Reflect -> HITL)
        │
        ├── A2A ──► L3 ChatAgent (Port 9100)
        ├── A2A ──► L3 RAGAgent (Port 9101)  ──► L5 LlamaIndex Hybrid RAG
        └── A2A ──► L3 CodingAgent (Port 9102) ──► L4 MCP Tools (9001, 9002, 9003)
        │
        ▼
L7 LiteLLM Gateway (Port 4000)
        ├── vLLM Engine (Port 8001, Qwen3-72B / DeepSeek-R1)
        └── llama.cpp Engine (Port 8080 chat, Port 8081 embeddings)
```

---

## Quickstart

### 1. Environment Setup
```bash
cp .env.example .env
make install
```

### 2. Start Supporting Infrastructure
```bash
make infra-up
```

### 3. Launch Services
Run MCP Servers:
```bash
make serve-mcp
```

Run A2A Sub-Agents:
```bash
make serve-agents
```

Run L1 FastAPI Application:
```bash
make dev
```

Visit the interactive API docs at `http://localhost:8000/docs`.
