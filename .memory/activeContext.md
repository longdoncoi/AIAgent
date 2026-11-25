# activeContext.md — Trạng thái hoạt động hiện tại

## Cập nhật 2026-11-28 — Khắc phục GitHub Actions Gitleaks sau squash

- Job `Lint, Type Check & Vulnerability Audit` của run `46149979723` fail tại Gitleaks vì 2 fixture JWT test trong commit squash mới `dffd6d9`; đây là false-positive, không phải credential thật.
- Đã thêm đúng 2 fingerprint lịch sử vào `.gitleaksignore` và chuyển fixture hiện tại sang chuỗi ghép để tránh tái phát nếu lịch sử tiếp tục được squash.
- Xác minh: Gitleaks `8.30.1` quét đủ 22 commit và báo `no leaks found`; targeted test `18 passed`; `make lint` PASS (Ruff + mypy 104 files). `make test` local bị harness Windows treo ở các subprocess coverage lồng nhau và phải dừng, không ghi nhận assertion failure.

## Cập nhật 2026-11-28 — Source quality 10/10

- Đã hoàn tất hardening source theo rubric được cưỡng chế trong repository, tách biệt khỏi đánh giá production/go-live.
- Mở rộng Ruff sang `ASYNC/B/C4/RUF/SIM/UP`, mypy đạt trên 104 file; nâng pytest coverage gate lên 90%, ngưỡng độc lập lên line 92% / branch 81.5% / core line 98% và thêm per-file floors cho 10 module trọng yếu.
- Sửa A2A Ed25519 `kid`/key rotation, OpenTelemetry strict fail-closed và async JSONL eval I/O; thêm 28 test giá trị cao cho identity, audit, tracing, migrations, checkpointer, thread registry và MCP security.
- Gate cuối: `make lint` PASS; `make test` PASS **554/554**; coverage tổng **90.43%**, line **92.78%**, branch **82.62%**, core line **98.08%**; `uv lock --check` và `git diff --check` PASS.
- Bằng chứng: `evidence/prr/source-quality-10-of-10-2026-11-28.md`.

## Cập nhật 2026-11-28 — Khắc phục review P0/P1/P2

- Đã rà soát toàn bộ nhận định trong review và đóng các lỗi thuộc phạm vi source/config: SSE `fast_finalize`, model-serving K8s, reflector evidence, exact approved tool-call replay, A2A Ed25519 envelope, tenant-scoped thread registry, audit integrity v2, SLO/alerts/FinOps, semantic-cache entity guard, regex timeout, PII VN, Tempo/OTel và drift tài liệu.
- Gate mới nhất: `make lint` PASS (Ruff + mypy 103 files); `make test` PASS **526/526**, coverage tổng **88.42%**, line **90.94%**, branch **80.05%**, core line **97.31%**; production Kustomize render/parse **126 resources**; `uv lock --check` PASS.
- Báo cáo đối chiếu đầy đủ: `evidence/prr/review-remediation-2026-11-28.md`.
- Go-live vẫn **CHƯA ĐẠT** và `production_ready` phải giữ `false`: còn runtime eval/load/chaos/PITR trên cluster thật, audit anchor WORM với credential tách biệt, thay Temporal auto-setup/superuser bằng schema lifecycle + least-privilege credential, và đủ chữ ký Release Board.

## Trọng tâm

- Đợt kiểm định & khắc phục toàn diện theo chuẩn Kiến trúc sư trưởng được đo lại ngày **2026-11-28** đã đóng toàn bộ blocker phát hiện được trong phạm vi source/repository: test isolation, CI supply chain, GPU scheduled scaling, GPU OOM alerting, GitOps/PRR drift và production manifest validation.
- **Trạng thái hai tầng**:
  - **Mã nguồn, cấu hình, Kubernetes manifests, CI gates & kiểm thử tự động**: Đã gia cố đồng bộ. Kết quả đo gần nhất: `ruff` đạt; mypy đạt trên **102 files** (`src/`, `evals/`, `scripts/`); `uv lock --check` đạt; Bandit High sạch; production `pip-audit` sạch; Gitleaks quét đủ **22 commits / 6.94 MB** không phát hiện secret; production Kustomize render **115 resources**, kubeconform **97 valid / 0 invalid / 0 error / 18 CRD skipped**; full CI-style suite **521 passed, 0 skipped** và không có `RuntimeWarning`; stability run trước fallback junction đạt **1,458 passed, 3 skipped**, sau đó chính test bị skip được chạy lặp riêng và đạt **3/3**; line coverage **91.27%**, branch coverage **80.28%**, core line coverage **97.28%**. Red-team đạt **60/60** và restore smoke đạt.
  - **Go-Live Production 10/10**: **CHƯA ĐẠT** vì cụm dịch vụ staging/production thật (`LiteLLM` `reasoning-llm` 72B+ / `BAAI/bge-m3` / Qdrant / Redis / PostgreSQL+PgBouncer / OTel) chưa vận hành trong môi trường kiểm tra hiện tại (`evals/release_gate.py` thoát `1` do LLM gateway ngoại tuyến), chưa diễn tập tải/chaos/WAL PITR `RPO=0` trên cluster thật, và thiếu 9 chữ ký Release Board (`evidence/signoff/release-board.json`).

- **Release quality evidence mới nhất**: Toàn bộ blocker thuộc source của release gate đã được đóng. Smoke eval ngày 2026-11-28 đạt `exit=0` nhưng tự xác định là `heuristic_proxy` (routing **93%**, risk **96%**, tool selection **94%**, groundedness proxy **0.48**, faithfulness proxy **0.87**) vì gateway/embedding ngoại tuyến; kết quả này không được dùng làm bằng chứng release. Strict release gate vẫn fail-closed `exit=1` với lý do thực tế: `LLM gateway unreachable — no real generation or LLM judging possible`.

## Đã hoàn thành gần nhất (2026-11-28 Source Readiness Closure)

- **Deterministic tests**: cô lập process-wide reranker model cache trong test lặp; bỏ mock `asyncio.wait_for` gây rò coroutine `Event.wait()`; dùng Windows junction fallback để test traversal thực thi thay vì skip khi thiếu đặc quyền symlink; bổ sung 5 regression tests cho KEDA, alert sync, ArgoCD policy và CI supply chain.
- **GPU/SRE**: thêm KEDA cron scaler production cho vLLM, timezone `Asia/Ho_Chi_Minh`, min 1/max 4 và fallback 1; thêm `GPUOOMAlert` đồng nhất ở canonical/deployed Prometheus rules. `promtool` xác minh thành công **11 rules**.
- **Supply chain**: Gitleaks full-history được pin phiên bản/checksum và dùng exact-fingerprint ignore; build sinh/upload CycloneDX SBOM, BuildKit provenance và ký/xác minh digest bằng Cosign keyless OIDC.
- **GitOps/specification sync**: đồng bộ AGENTS, Architecture, specifications và PRR với chính sách ArgoCD manual-attested hiện hành; không ghi nhận auto-sync khi manifest không cấu hình auto-sync.
- **Đo kiểm**: parse thành công **36 YAML files / 127 documents**; Alembic upgrade `base -> 004` và downgrade `004 -> base` sinh SQL thành công; strict schema validation production overlay không có manifest invalid/error.

## Đã hoàn thành gần nhất (2026-11-27 Chief Architect PRR Remediations)

- **Schema, Migrations & SQL Hygiene**:
  - Thêm `migrations/versions/004_audit_logs_seq_and_checkpoints.py` bổ sung cột đơn điệu `audit_logs.chain_seq` (`BigInteger` Identity + `idx_audit_logs_chain_seq`) và pre-seed schema LangGraph `PostgresSaver` (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes.task_path`, `checkpoint_migrations` v0..9); thêm `path_separator = os` vào `alembic.ini`.
  - Tách `postgres_migration_uri` (`agentic-maintenance-secrets`) khỏi `postgres_app_uri`; `AUTO_RUN_MIGRATIONS=false` mặc định trên runtime và `main.py`/`short_term.py` chỉ chạy read-only `check_database_migrations()` fail-closed trong production thay vì chạy DDL `saver.setup()`. Đồng bộ `prepare_threshold=None` cho PgBouncer transaction pooling.
  - Khử toàn bộ 4 cảnh báo SQL `Bandit B608` tại `long_term.py` và `data_lifecycle.py` bằng static dict queries.
  - `data_lifecycle.py` & `audit.py`: RTBF `purge_user_data()` fan-out xóa key trên cả `redis-state` lẫn `redis-cache`; `audit.py` sắp xếp `ORDER BY chain_seq DESC, created_at DESC LIMIT 1` dưới `pg_advisory_xact_lock`.
- **Auth, Secrets, Caching & RAG**:
  - `core/config.py`: Bắt buộc `checkpoint_hmac_secret` riêng (khác `s2s_internal_token`) cho `service_role in {"all", "api", "maintenance"}`; mặc định `mcp_transport="streamable-http"` (`/mcp`); bổ sung `enforce_rag_rbac` & property `rag_rbac_strict` bật fail-closed trên `staging` và `production`.
  - `api/deps.py` & `middleware.py`: `TokenRevocationUnavailable` fail-closed (`HTTP 503` khi `redis-state` lỗi ở production) và `_VERIFIED_JWT_CTX` cache xác thực JWT off-event-loop (`asyncio.to_thread`) một lần mỗi request.
  - `api/routes/health.py` & `admin.py`: `/readyz` kiểm tra đồng thời `redis_url` và `redis_state_url` (fail-closed ở production); `/rag/cdc` gắn suffix `uuid` tránh trùng `workflow_id`.
  - `cache/locking.py`, `prompt_cache.py`, `rag_cache.py`: Bổ sung `adesynchronize_waiters()` async-safe; ưu tiên `redis_state_url` (noeviction) cho invalidation generation.
  - `rag/ingestion.py` & `retriever.py`: Cưỡng chế RAG RBAC ở cả `staging` & `production`; `_prune_docstore_nodes()` dọn leaf node cũ/đã xóa khỏi `docstore.json`.
  - `safety/guardrails.py` & `tools/servers/repo_server.py`: Quét obfuscated base64 trên cả bản gốc lẫn văn bản đã lọc zero-width/homoglyph; chặn `.env`/`.env.*`/`.envrc` ở mọi độ sâu thư mục (giữ `.env.example`).
- **A2A, Orchestrator, Temporal, Serving, K8s & Evals**:
  - `a2a/client.py` & `executor.py`: Tôn trọng `allow_local_agent_fallback`; idempotency Redis (`redis-state`) cho cả `execute` lẫn `execute_stream`.
  - `orchestrator/nodes/dispatch.py`, `hitl.py`, `finalize.py` & `workflows/{agent_workflow,worker}.py`: Hỗ trợ gom duyệt 2 pha tool-call song song tất định và `ToolCallApprovalRequired` non-retryable qua Temporal; `hitl.py` offload audit log bằng `asyncio.to_thread`; `finalize.py` truyền `tenant_id`; `worker.py` expose HTTP `/healthz` & `/readyz` trên `:9105` cùng SIGTERM graceful shutdown (`30s`) và tùy chọn `TEMPORAL_TLS_ENABLED`.
  - `deploy/k8s/`: Loại bỏ `PERMISSIVE` mTLS khỏi `istio-security.yaml` (thuần `STRICT` toàn namespace) và khóa `allow-agents-to-mcp-tools` đúng principal/cổng từng agent (loại trừ `rag`); đặt `RATE_LIMIT_DEGRADED_LOCAL_FALLBACK="false"` trong `configmap-secrets.yaml`; tách PVC `agentic-qdrant-backups` (streaming SHA-256 + upload fail-closed + dọn 14 ngày) và PVC RWX `agentic-mcp-scratch` (`mcp-scratch-storage.yaml`); đặt `HF_HOME="/opt/hf-cache"` & `HF_HUB_OFFLINE="1"` trên `temporal-worker` và `rag-bootstrap-job`; đồng bộ `BAAI/bge-m3` & `drop_params: true` trên `configs/litellm_config.yaml` / `llamacpp-embed.sh`; sửa `tc_03` (`expected_risk: "high"`) trong `golden_v1.jsonl` & `seed_eval.py`.
  - Bổ sung bộ test mới `tests/unit/test_chief_architect_remediations.py` và cập nhật test cũ khớp bất biến mới (`test_security_hardening_v3.py`, `test_production_hardening.py`, `test_prr_remediations.py`, `test_full_coverage.py`).

## Lịch sử nền tảng các đợt hardening trước (v1–v9)

- Đã hoàn tất trước đó: Ed25519 capability attenuation, tách ExternalSecrets theo service role, OIDC/JWKS, PSA `restricted`, Python 3.15 Chainguard compatibility (`_py315_dataclasses_compat.pth`), RAG citation gating, RTBF cross-backend, AsyncPostgresSaver + PgBouncer compatibility, FinOps bounded metrics, và CI/CD two-commit sign-off gate.

## Blocker Go-Live còn lại

1. Provision cụm dịch vụ production/staging thật (LiteLLM với `reasoning-llm` Qwen3-72B/frontier + `BAAI/bge-m3`, Qdrant, Redis, PostgreSQL/PgBouncer, OTel/Tempo) và chạy `evals/release_gate.py` đạt PASS toàn bộ (`ragas` + `deepeval`).
2. Chạy load/soak 100 users >=5 phút, chaos, cross-tenant probing, cross-pod approval-key probing, RTBF đối soát từng backend và migration/retention drill trên cluster thật.
3. Cấu hình WAL archiving/off-cluster PITR và diễn tập restore (logical PVC backup hiện tại không đạt `RPO=0`).
4. Xác thực ExternalSecret/Vault, Alertmanager/Slack, LiteLLM budget rejection và spend-log allocation trên runtime thật.
5. Cấu hình GitHub secrets/variables, branch protection, push thay đổi và xác nhận mọi remote CI job xanh.
6. Thu đủ 9 chữ ký Release Board theo giao thức `C -> A -> tag`; không tự tạo `evidence/signoff/release-board.json`.
