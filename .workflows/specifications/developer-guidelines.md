# Developer map

Tệp này định tuyến từ loại thay đổi đến source-of-truth; không lặp runbook.

| Thay đổi | Implementation owner | Hướng dẫn |
| --- | --- | --- |
| API/SSE/approval | `src/agentic/api/` | [`../coding-workflow.md`](../coding-workflow.md), [`safety-hitl.md`](safety-hitl.md) |
| Supervisor/state/DAG | `src/agentic/orchestrator/` | [`agent-specifications.md`](agent-specifications.md) |
| A2A agent | `src/agentic/agents/`, `a2a/` | [`../../.skills/add-subagent/SKILL.md`](../../.skills/add-subagent/SKILL.md) |
| MCP tool | `src/agentic/tools/`, `configs/policies.yaml` | [`../../.skills/add-mcp-tool/SKILL.md`](../../.skills/add-mcp-tool/SKILL.md) |
| RAG | `src/agentic/rag/` | [`protocols-networking.md`](protocols-networking.md) |
| Persistence | `src/agentic/memory/`, `migrations/` | [`state-memory.md`](state-memory.md), [`../../.skills/database-migration/SKILL.md`](../../.skills/database-migration/SKILL.md) |
| Eval/observability | `evals/`, `src/agentic/observability/` | [`observability-evals.md`](observability-evals.md) |
| Deploy | `deploy/`, `.github/workflows/` | PRR checklist liên quan trong `../reviews/` |

## Sources of truth

- Python/dependencies: `pyproject.toml`, `uv.lock`
- Commands: `Makefile`
- Runtime settings/URLs: `src/agentic/core/config.py`, `.env.example`
- Tool risk/allowlist: `configs/policies.yaml`
- Eval gates: `configs/eval_thresholds.yaml`
- Production topology: `deploy/docker/`, `deploy/k8s/`

Thực hiện theo `AGENTS.md`; chạy kiểm tra từ skill tương ứng và cập nhật specification chỉ khi contract thực sự đổi.
