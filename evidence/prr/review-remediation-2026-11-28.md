# Review remediation — 2026-11-28

## Verdict

- Source/configuration readiness: PASS for the reviewed repository scope.
- Production go-live: remains `false`. Runtime quality evidence, real-cluster drills,
  immutable external audit anchoring, and Release Board signatures are external
  evidence and must not be fabricated by a source-code change.

## P0

| Finding | Result | Evidence / disposition |
|---|---|---|
| SSE fast path emits no answer | Fixed | `chat.py` emits guarded token/final events for `fast_finalize`; regression test added. |
| K8s model serving is incomplete | Fixed in manifests | LiteLLM config is mounted and selected; pod-safe service URLs replace localhost; reasoning/fast/coding/backup workloads exist; GPU, tensor parallelism, tool parser and prefix caching are configured; images are immutable. |
| Existing evidence does not prove production | Confirmed, intentionally still open | Local gates now pass, but real model/embedding, load/chaos/PITR evidence and nine Release Board signatures are still required. `evidence/prr/latest.json` remains honest. |

## P1

| Finding | Result | Evidence / disposition |
|---|---|---|
| Reflector sees only a 200-character prefix | Fixed | Bounded 8,000-character evidence per step includes input and output and is explicitly marked untrusted. |
| Approved tool call is regenerated; TTL too short | Fixed | HITL stores the exact tool/arguments/digest, dispatch carries it, executor invokes that exact call; TTL is 300 seconds. |
| Tenant ACL may omit tenant principal | Verified already safe | Existing tenant ACL path and non-public tenant tests pass. |
| Audit does not sign trace/risk; timezone unstable | Fixed | Integrity v2 binds trace/risk and normalizes PostgreSQL timestamps to UTC, with legacy verification support. |
| Audit has no external WORM anchor | Confirmed, external control remains | A WORM/object-lock target and separate credentials must be provisioned outside this repository before go-live. |
| SLO/alerts/early-response metrics/backup alert drift | Fixed | 0.1% threshold and fast-burn alert, 5xx-only SLO errors, early 413/429/503 observations, and backup failure alerts are present. |
| Supervisor model spend is unattributed | Fixed | Chat invoke/stream establishes `ToolExecutionContext` around the supervisor graph. |
| STRICT mesh conflicts with sidecar-less jobs | Fixed | Migration, backup, retention and bootstrap jobs inject sidecars with startup hold and short drain. |
| Bootstrap may download at runtime | Verified mitigated | Neural model is pre-baked and runtime workloads use offline HF cache settings. |
| Temporal auto-setup shares privileged database credentials | Confirmed, not safely auto-migrated | Replacing this requires a dedicated Temporal schema lifecycle and externally provisioned least-privilege credentials. It remains a go-live infrastructure blocker. |
| Qdrant replication factor 2 with one pod | Fixed | Single-pod base uses replication factor 1; production HA must scale topology and factor together. |
| Indirect prompt injection / LTM poisoning / entity cache collision | Fixed | Planner sanitizes LTM, reflector treats evidence as untrusted, and semantic cache preserves named-entity distinctions. |
| A2A routing identity is unsigned | Fixed | Ed25519 envelope covers routing identity, payload and metadata, with freshness validation and strict fail-closed verification. |

## P2

| Finding | Result | Evidence / disposition |
|---|---|---|
| Failed idempotency lock leaks | Fixed | Lock entries are removed after cancellation/storage failure. |
| Global thread ID squatting | Fixed | Registry and checkpoint identities include tenant + owner scope. |
| Singleton-bound LLM bypasses circuit breaker | Fixed | Bound clients re-evaluate the breaker before every real model invocation; cache hits do not call the model. |
| PII masks arbitrary 10-digit codes | Fixed | Only international or valid Vietnamese mobile prefixes are masked, including spaced forms. |
| Repository regex can ReDoS | Fixed | Explicit `regex` dependency and per-search timeout are enforced. |
| Istio egress policy is actually ingress | Fixed | Misleading AuthorizationPolicy removed; Kubernetes NetworkPolicy is the egress control. |
| Reranker revision is unpinned | Fixed | Runtime and image prefetch use an immutable Hugging Face revision. |
| Strict JWT/CORS accepts HS256 or loopback | Fixed | Staging/production reject symmetric JWT algorithms and loopback CORS; tests use RS256/JWKS. |
| Deleted documents leave vectors | Verified already fixed | Ingestion deletion path removes indexed point IDs; regression coverage remains green. |
| Detailed OTel logs and no Tempo | Fixed | Logging exporter uses basic verbosity; Tempo deployment/service/config and collector egress are rendered. |
| BaseHTTPMiddleware should be replaced solely because SSE is used | Not accepted as a demonstrated defect | Current middleware accounts for full streaming duration and early responses, and SSE lifecycle tests pass. A pure-ASGI rewrite is a larger refactor without a reproduced correctness failure. |

## Documentation drift

- A2A is described honestly as A2A-inspired REST/SSE, not the JSON-RPC SDK.
- Architecture/model documentation matches the Qwen2.5 serving config.
- Neo4j is documented as optional and disabled in production.
- SQLite LTM recall retrieves a bounded candidate set and ranks tokens instead of
  using `LIKE '%<whole question>%'`.

## Verification

- `make lint`: PASS; Ruff clean; mypy clean across 103 source files.
- `make test`: PASS; 526 passed; total 88.42%, line 90.94%, branch 80.05%, core line 97.31%.
- Targeted review regressions: 67 passed.
- `uv lock --check`: PASS.
- `kubectl kustomize deploy/overlays/production`: PASS; 126 parsed resources.

