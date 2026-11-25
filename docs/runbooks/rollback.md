# Production Rollback Runbook

> **Scope:** Rolling back a faulty application release (`agentic-api`, L3 Sub-Agents, L4 MCP tools) in `agentic-platform` without data loss or checkpoint corruption.  
> **RTO Target:** < 10 minutes via ArgoCD GitOps revert.

---

## 1. Decision Criteria (Trigger Rollback Immediately If Any Apply)

- `HighAPIErrorRate`, `HighLLMLatencyP99`, or `ModelCircuitBreakerTripped` firing for > 2 minutes post-deploy.
- `/readyz` fails on `agentic-api` pods (`503 Service Unavailable`) due to broken startup dependency or schema mismatch.
- Uncontrolled increase in `ModelCircuitBreakerTripped` or `REFLECTION_FALLBACK_TOTAL`.

---

## 2. Step 1 — Freeze Automated Sync & Identify Last Good Revision

```bash
kubectl -n agentic-platform get deploy -o wide
argocd app get agentic-platform-prod
argocd app history agentic-platform-prod
```

Record the last healthy `REVISION` ID and Git SHA.

---

## 3. Step 2 — Execute GitOps Rollback (Preferred)

Always revert in Git so ArgoCD self-heal does not re-apply the broken commit:

```bash
# Revert the bad release commit on the deployment branch
git revert --no-edit <BAD_COMMIT_SHA>
git push origin <RELEASE_BRANCH>

# Trigger immediate ArgoCD reconciliation
argocd app sync agentic-platform-prod --prune=false
argocd app wait agentic-platform-prod --health --timeout 300
```

Emergency manual rollback (only when Git push is blocked; re-enable GitOps revert immediately afterward):

```bash
kubectl -n agentic-platform rollout undo deployment/agentic-api deployment/agentic-chat-agent deployment/agentic-rag-agent deployment/agentic-coding-agent
```

---

## 4. Step 3 — Database & Cache Consistency Checks

- **Schema Compatibility:** Never run destructive `alembic downgrade` during an active incident if forward-compatible columns exist. Verify with:
  ```bash
  kubectl -n agentic-platform exec deploy/agentic-api -- python scripts/migrate_db.py --check
  ```
- **Point-in-Time Recovery (PITR):** Only restore Postgres WAL backup (`deploy/db/postgresql.conf` / managed cloud PITR) if corrupt rows were committed by an untested migration.
- **Prompt / RAG Cache Invalidation (if stale/bad embeddings or prompts were cached):**
  ```bash
  kubectl -n agentic-platform exec deploy/agentic-api -- python -c "import asyncio; from src.agentic.core.redis import get_redis_client; print('connected')"
  ```
  Flush only `prompt_cache:*` or `rag_cache:*` keys via targeted `SCAN` + `UNLINK` on the cache Redis (`REDIS_URL` / `redis-cache`) — **never** `FLUSHALL` on the state Redis (`REDIS_STATE_URL` / `redis-state` with `noeviction`) backing `agentic:checkpoint:*`, approval nonces, thread ownership, and JTI revocations.

- **Backup Restore Drill Verification:** Execute `python scripts/restore_drill.py` regularly against offsite database dumps and Qdrant snapshots to verify RPO/RTO compliance before major releases.

---

## 5. Step 4 — Post-Rollback Verification

1. Confirm all pods `Ready`:
   ```bash
   kubectl -n agentic-platform get pods
   ```
2. Verify readiness probe against `agentic-api-svc`:
   ```bash
   kubectl -n agentic-platform exec deploy/agentic-api -- \
     python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=5).read())"
   ```
3. Confirm Prometheus alerts cleared (`HighAPIErrorRate`, `HighLLMLatencyP99`, `ModelCircuitBreakerTripped`) and archive incident timeline in the postmortem tracker.
