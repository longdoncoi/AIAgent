---
name: add-subagent
description: Add or change a stateless L3 sub-agent served over A2A, including its graph, prompt, AgentCard, endpoint/settings, Supervisor routing, tool allowlist, deployment wiring, and contract tests. Use for work under src/agentic/agents or A2A agent registration.
---

# Add or change an A2A sub-agent

1. Search an existing agent name across `src/`, `configs/`, `deploy/`, `Makefile` and tests; mirror the complete integration surface rather than copying a stale snippet.
2. Create `src/agentic/agents/<name>/graph.py` with a cached graph built through `build_react_graph(model, tools, prompt)` from `agents/base.py`.
3. Add the prompt at `configs/prompts/<name>_agent.md`; keep role, allowed behavior and output contract concise.
4. Define an `AgentCard` constant and aliases in `src/agentic/a2a/cards.py`.
5. Create `server.py` using `create_a2a_server(card=..., graph=...)`; keep the service stateless.
6. Register URL/settings and Supervisor routing wherever existing agents are mapped. Add only the MCP tools required by the role to `configs/policies.yaml`.
7. Allocate a non-conflicting port and update `Makefile`, Docker Compose/Kubernetes service, probes, NetworkPolicy and observability labels when deployment changes.
8. Test card discovery, A2A request/stream contract, routing, tool denial, timeout/failure and execution-context propagation. Run targeted unit/integration tests before broader gates.

Preserve owner/run/step metadata and capability verification. Never add local production fallback that bypasses A2A isolation.
