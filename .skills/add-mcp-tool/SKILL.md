---
name: add-mcp-tool
description: Add or change an MCP/FastMCP tool, its Pydantic input schema, policy risk classification, per-agent allowlist, transport registration, security boundary, and tests. Use for work in src/agentic/tools, MCP server endpoints, or configs/policies.yaml.
---

# Add or change an MCP tool

1. Inspect one existing tool end to end: server core function/decorator, `src/agentic/tools/mcp_client.py`, `configs/policies.yaml`, tests and deployment wiring.
2. Implement a typed core function in the correct server. Keep sync filesystem work out of the event loop and bound execution by timeout/resource limits.
3. Add/update the Pydantic input model, description, local binding and remote server routing in `mcp_client.py`.
4. Add the tool to `configs/policies.yaml` under `tools` and the minimum required `agent_allowlist` entries.
5. Classify risk from effect, not name:
   - low: read-only and bounded;
   - medium: isolated execution without durable write;
   - high: shell, durable write, git mutation, deletion or external side effect.
6. Require HITL for high-risk tools. Preserve S2S authentication and step-scoped capability verification at the MCP server boundary; client-side checks alone are insufficient.
7. If adding a server/process, update settings, `Makefile`, Docker/Kubernetes service wiring, network policy and the port map. Reuse an existing server when isolation does not require a new process.
8. Test schema rejection, allow/deny policy, approval requirement, remote/local behavior, timeout and failure propagation. Run targeted tests, then gates appropriate to the touched boundary.

Do not return exception text that leaks secrets or silently convert authorization failures into successful tool output.
