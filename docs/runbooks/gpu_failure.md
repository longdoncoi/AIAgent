# GPU Inference & vLLM Failover Runbook

> **Scope:** Handling vLLM GPU node failures, CUDA OOM, or `ModelCircuitBreakerTripped` alerts when primary `reasoning-llm` / `coding-llm` endpoints become unavailable.

---

## 1. Automated Failover Behavior (Verified Architecture)

1. `LiteLLM` (`configs/litellm_config.yaml`) and `src/agentic/llm/gateway.py` route primary `reasoning-llm` traffic to vLLM (`deploy/serving/vllm.sh`).
2. When primary vLLM errors exceed threshold or time out, requests trip the circuit breaker and fail over to the CPU `llama.cpp` backup alias (`llama-chat-backup`, served via `deploy/serving/llamacpp-chat.sh`).
3. Prometheus rule `ModelCircuitBreakerTripped` (`deploy/monitoring/alerts.yaml`) fires (`severity: critical`) when either:
   - `increase(agent_model_proxy_fallback_total[5m]) > 0` (server-side transparent fallback returned by LiteLLM, e.g. `llama-chat-backup` / `llama-server` / downgraded model), OR
   - `increase(agent_tokens_total{model_alias="llama-chat-backup"}[5m]) > 0` AND `rate(agent_tokens_total{model_alias="reasoning-llm"}[5m]) == 0` (client-side circuit-breaker failover).

---

## 2. Step 1 — Triage & Confirm Degraded Mode

```bash
# Check vLLM GPU pod logs & node conditions
kubectl -n agentic-platform get pods -l app=vllm
kubectl -n agentic-platform describe pod -l app=vllm | tail -n 50
kubectl get nodes -l nvidia.com/gpu.present=true

# Verify LiteLLM health & active model routes
kubectl -n agentic-platform exec deploy/agentic-api -- \
  python -c "import urllib.request; print(urllib.request.urlopen('http://litellm-svc:4000/health/liveliness', timeout=5).read())"
```

Confirm CPU fallback (`llama-chat-backup`) is serving degraded responses cleanly without cascading `5xx` errors on `agentic-api`.

---

## 3. Step 2 — Protect Degraded Capacity

While running on CPU `llama.cpp` (`CTX_SIZE=8192`, lower tokens/sec):

1. Monitor `HighLLMLatencyP99` and `agent_request_duration_seconds_bucket`.
2. Temporarily lower ingress rate limits or pause batch ingestion / background evaluation jobs so interactive user sessions retain priority.
3. Verify HITL queue (`agent_hitl_pending_approvals`) does not silently drop waiting approvals.

---

## 4. Step 3 — Remediate GPU Node / vLLM Service

Common causes and remediation:
- **CUDA OOM:** Reduce `--gpu-memory-utilization` slightly (e.g. `0.90` -> `0.85`) or check `--max-model-len` (`32768`) vs KV cache headroom before restarting vLLM.
- **NVML / GPU Hardware Fault / Xid Error:** Cordon and drain the unhealthy GPU node (`kubectl cordon <node> && kubectl drain <node> --ignore-daemonsets --delete-emptydir-data`) so vLLM reschedules onto a healthy GPU node.
- **vLLM Pod CrashLoop:** Inspect startup model download / HF token secrets and restart rollout (`kubectl -n agentic-platform rollout restart deployment/vllm`).

---

## 5. Step 4 — Verify Full Recovery

1. Wait for vLLM `/health` and test completion on `reasoning-llm`.
2. Confirm `agent_tokens_total{model_alias="reasoning-llm"}` resumes > 0, `agent_model_proxy_fallback_total` stops increasing, and `ModelCircuitBreakerTripped` resolves.
3. Re-enable deferred batch jobs and record GPU incident metrics in the reliability log.
