# REVIEW.md — Cẩm Nang & Checklist Đánh Giá Độ Sẵn Sàng Production (PRR)

> **Tài liệu kiểm định kỹ thuật chính thức cấp quản lý** cho Hội đồng Đánh giá Phát hành (Release Board).  
> **Dự án**: `agentic-platform` (Multi-Agent Doanh Nghiệp Phân Cấp: LangGraph · vLLM · llama.cpp · LlamaIndex · MCP · A2A · Temporal · Kubernetes)  
> **Phiên bản tài liệu**: `v1.0.0-PROD-AUDIT` · **Tài liệu tham chiếu cơ sở**: [Architecture.md](../../Architecture.md) · [AGENTS.md](../../AGENTS.md)

---

## 1. Tổng Quan Khung Đánh Giá PRR (Executive Overview)

Tài liệu này đóng vai trò **Bộ chỉ huy trung tâm (Master Orchestrator)** điều phối toàn bộ tiêu chuẩn đo lường và quy trình kiểm định trước khi kích hoạt phát hành Production.  
Toàn bộ hệ thống kiểm định gồm **14 Trụ cột Kỹ thuật** và **4 Khung Quản trị Vận hành** được quản lý độc lập tại thư mục [.workflows/reviews/](.):
- **Điều kiện Go-Live tối thiểu**: 100% tiêu chí P0 đạt (`44/44`), $\ge 90\%$ tiêu chí P1 đạt (`25/27`), Điểm tổng thể $\ge 95.0\%$. Chỉ cần **1 lỗi P0** chưa giải quyết $\rightarrow$ **Lập tức No-Go** (không ngoại lệ).

---

## 2. Bảng Tóm Tắt Trạng Thái Sẵn Sàng (Executive Readiness Dashboard)

| STT | Trụ Cột Kỹ Thuật Đánh Giá | Tệp Đặc Tả Chuyên Biệt | Đơn Vị Phụ Trách | Số Tiêu Chí (P0/P1/P2) | Trọng Số | Trạng Thái P0 |
| :---: | :--- | :--- | :--- | :---: | :---: | :---: |
| **01** | **Kiến Trúc & Giao Thức** | [REVIEW_ARCHITECTURE & PROTOCOLS.md](REVIEW_ARCHITECTURE%20&%20PROTOCOLS.md) | Lead Architect & Backend Lead | 6 (4 P0 / 2 P1) | 8% | [ ] 4/4 P0 |
| **02** | **Bộ Nhớ & Dữ Liệu** | [REVIEW_STATE, MEMORY & PERSISTENCE.md](REVIEW_STATE,%20MEMORY%20&%20PERSISTENCE.md) | Lead Backend & DBA | 6 (3 P0 / 3 P1) | 8% | [ ] 3/3 P0 |
| **03** | **Bộ Đệm Đa Tầng** | [REVIEW_MULTI-TIER CACHING SUBSYSTEM.md](REVIEW_MULTI-TIER%20CACHING%20SUBSYSTEM.md) | Performance & Backend Lead | 4 (2 P0 / 2 P1) | 7% | [ ] 2/2 P0 |
| **04** | **Bảo Mật & HITL** | [REVIEW_SECURITY, GUARDRAILS & HITL.md](REVIEW_SECURITY,%20GUARDRAILS%20&%20HITL.md) | SecOps / Cyber Security Lead | 6 (5 P0 / 1 P1) | 10% | [ ] 5/5 P0 |
| **05** | **Cổng Mô Hình (LLM)** | [REVIEW_MODEL GATEWAY & INFERENCE.md](REVIEW_MODEL%20GATEWAY%20&%20INFERENCE.md) | AI Serving / Infra Lead | 4 (2 P0 / 2 P1) | 8% | [ ] 2/2 P0 |
| **06** | **Chất Lượng Tác Tử (AI)**| [REVIEW_QUALITY GATES - DEEPEVAL & RAGAS.md](REVIEW_QUALITY%20GATES%20-%20DEEPEVAL%20&%20RAGAS.md) | AI Evaluation & Data Quality | 5 (3 P0 / 2 P1) | 10% | [ ] 3/3 P0 |
| **07** | **Kubernetes & SRE** | [REVIEW_KUBERNETES & SRE.md](REVIEW_KUBERNETES%20&%20SRE.md) | Principal SRE & Cloud Architect | 7 (5 P0 / 2 P1) | 8% | [ ] 5/5 P0 |
| **08** | **Giám Sát & Cảnh Báo** | [REVIEW_OBSERVABILITY & ALERTING.md](REVIEW_OBSERVABILITY%20&%20ALERTING.md) | SRE Monitoring & Backend Lead | 4 (2 P0 / 2 P1) | 7% | [ ] 2/2 P0 |
| **09** | **CI/CD & GitOps** | [REVIEW_CI-CD & GITOPS.md](REVIEW_CI-CD%20&%20GITOPS.md) | DevOps / Release Lead | 5 (4 P0 / 1 P1) | 5% | [ ] 4/4 P0 |
| **10** | **Vận Hành & Khôi Phục** | [REVIEW_SRE & DISASTER RECOVERY.md](REVIEW_SRE%20&%20DISASTER%20RECOVERY.md) | Incident Commander & Lead SRE | 4 (2 P0 / 2 P1) | 5% | [ ] 2/2 P0 |
| **11** | **Chiến Lược Test & QA** | [REVIEW_TESTING & QA STRATEGY.md](REVIEW_TESTING%20&%20QA%20STRATEGY.md) | QA Lead & Principal Tester | 6 (4 P0 / 2 P1) | 8% | [ ] 4/4 P0 |
| **12** | **Pháp Lý & Đạo Đức AI**| [REVIEW_COMPLIANCE, PRIVACY & AI ETHICS.md](REVIEW_COMPLIANCE,%20PRIVACY%20&%20AI%20ETHICS.md) | Legal Counsel & DPO Lead | 6 (4 P0 / 2 P1) | 6% | [ ] 4/4 P0 |
| **13** | **Quản Trị Chi Phí (FinOps)**| [REVIEW_FINOPS & COST GOVERNANCE.md](REVIEW_FINOPS%20&%20COST%20GOVERNANCE.md) | FinOps Lead & Cloud Architect | 5 (2 P0 / 2 P1 / 1 P2) | 5% | [ ] 2/2 P0 |
| **14** | **Quản Trị Tri Thức RAG**| [REVIEW_DATA GOVERNANCE & RAG RBAC.md](REVIEW_DATA%20GOVERNANCE%20&%20RAG%20RBAC.md) | Data Governance & RAG Lead | 5 (2 P0 / 2 P1 / 1 P2) | 5% | [ ] 2/2 P0 |
| **TỔNG**| **14 TRỤ CỘT KỸ THUẬT**| **14 TỆP CHUYÊN BIỆT** | **HỘI ĐỒNG THẨM ĐỊNH (9 LEADS)** | **73 TIÊU CHÍ (44 P0 / 27 P1 / 2 P2)**| **100%** | **[ ] 44/44 P0** |

---

## 3. Khung Quy Trình Quản Trị, Diễn Tập & Nghiệm Thu (Governance & Operational Domains)

Bên cạnh 14 trụ cột kỹ thuật, quy trình kiểm định và vận hành phát hành được chuẩn hóa thành 4 domain nghiệp vụ chuyên biệt:

| STT | Domain Quản Trị & Vận Hành | Tài Liệu Chi Tiết | Đơn Vị Chủ Trì | Nội Dung Trọng Tâm & Nhiệm Vụ |
| :---: | :--- | :--- | :--- | :--- |
| **01** | **Quy Chuẩn Kiểm Định & Phân Quyền RACI** | [REVIEW_GOVERNANCE & AUDIT STANDARDS.md](REVIEW_GOVERNANCE%20&%20AUDIT%20STANDARDS.md) | Chief AI Architect & SecOps Lead | Tiêu chuẩn đo lường, phân cấp rủi ro P0-P3, 4 phương pháp kiểm chứng (AST, Chaos, Probing) và ma trận RACI 9 vị trí. |
| **02** | **Quy Trình 5 Cổng Diễn Tập Trước Giờ G** | [REVIEW_PRE-FLIGHT STAGE GATES.md](REVIEW_PRE-FLIGHT%20STAGE%20GATES.md) | DevOps & QA Engineering Lead | Điều phối diễn tập tuần tự qua 5 Stage Gates: Chuẩn mã nguồn $\rightarrow$ Kiểm thử kháng lỗi $\rightarrow$ Chất lượng AI $\rightarrow$ Dữ liệu $\rightarrow$ Sức chịu tải E2E. |
| **03** | **Quản Trị Giám Sát Sau Triển Khai (Hypercare)** | [REVIEW_HYPERCARE & INCIDENT ESCALATION.md](REVIEW_HYPERCARE%20&%20INCIDENT%20ESCALATION.md) | Incident Commander & Principal SRE | Khung điều hành giám sát tăng cường 7 ngày (`T+15m`, `T+1h`, `T+24h`, `T+7d`), SLA phản hồi On-call và quy trình leo thang sự cố P0/P1. |
| **04** | **Biên Bản Nghiệm Thu & Phê Chuẩn Go-Live** | [REVIEW_SIGN-OFF & GO-LIVE APPROVAL.md](REVIEW_SIGN-OFF%20&%20GO-LIVE%20APPROVAL.md) | Hội Đồng Đánh Giá (Release Board) | Tổng kết điểm số, bảng quyết định phát hành (Go / Waiver / No-Go) và thủ tục ký duyệt của 9 đại diện bộ phận. |

---

*Tài liệu này là Bộ chỉ huy trung tâm (Master Orchestrator). Mọi thay đổi về tiêu chí kiểm định bắt buộc phải cập nhật đồng bộ tại các tệp chuyên biệt trong thư mục [.workflows/reviews/](.) trước khi đệ trình Hội đồng Đánh giá.*

---

## 6. Bằng Chứng Kiểm Định Tự Động & Phê Chuẩn (Auto-Evidence & Sign-off)

### 6.1 Trạng Thái Bằng Chứng

> **AUTO-EVIDENCE PENDING** — **Chờ đối soát và ký bởi Release Board.** Không có trạng thái `PASSED` nào được tự động gán.

- Artifact máy tạo: **[evidence/prr/latest.json](../../evidence/prr/latest.json)** — chứa `commit_sha`, `timestamp_utc`, exit code và output của từng lệnh `ruff`, `mypy --no-incremental`, full `pytest` kèm line/branch/core coverage gates, `eval --mode smoke`, `red_teaming_runner.py`, `restore_drill.py --smoke`, parse YAML và render production Kustomize overlay.
- Lệnh sinh artifact: `python scripts/generate_prr_evidence.py` (hoặc `make prr-evidence`). Script chạy kiểm định toàn diện ở mức mã nguồn, nhưng **không** tự gán nhãn phát hành; verdict luôn là `AUTO-EVIDENCE PENDING REVIEW BOARD` (`production_ready: false`) cho đến khi chạy `make eval-release` trên hạ tầng thật và có chữ ký Hội đồng.
- CI upload artifact này dưới tên `prr-evidence-<sha>` và job phát hành tải lại đúng artifact của workflow hiện tại; bản `evidence/prr/latest.json` đang được track trong Git không được dùng thay thế. Pipeline **không** tự động deploy nếu thiếu artifact sign-off (xem `evidence/signoff/release-board.json` và bước `Verify Release Sign-off` trong `.github/workflows/ci.yml`).
- Release gate chất lượng là lệnh riêng `make eval-release` (`evals/release_gate.py`), **không** phải smoke eval. Chỉ release gate mới được phép đưa ra kết luận "Ready for Production", và nó từ chối mọi proxy/heuristic.

### 6.2 Các Tiêu Chí Chưa Có Code (Truthful Gaps)

Các hạng mục dưới đây được ghi rõ trạng thái triển khai và phần bằng chứng vận hành còn thiếu thay vì để trống — Hội đồng cần nhìn đúng thực trạng:

| Tiêu chí | Tệp review | Trạng thái thực tế |
| :--- | :--- | :--- |
| Xóa dữ liệu theo yêu cầu (Right to be Forgotten) | `REVIEW_COMPLIANCE, PRIVACY & AI ETHICS.md` (12.1) | **IMPLEMENTED (code + test), chờ diễn tập staging** — purge fail-closed theo owner trên checkpoint/Redis/LTM, đồng thời xóa Qdrant, Knowledge Graph, cache và raw files theo tenant. |
| Citations có cấu trúc (`doc_id`, `page`, `source`) | `REVIEW_COMPLIANCE, PRIVACY & AI ETHICS.md` (12.4) | **IMPLEMENTED (code + test), chờ release eval** — `search_policy` phát block `[CITATIONS]`; Finalize node chỉ tổng hợp citation đã xác minh và từ chối trả lời RAG thiếu nguồn. |
| RBAC tài liệu theo `user_roles` / `department` | `REVIEW_DATA GOVERNANCE & RAG RBAC.md` (14.1) | **IMPLEMENTED (code + test), chờ kiểm chứng staging** — claim JWT được pin vào execution context ký A2A; Qdrant pushdown và lớp lọc phòng thủ áp dụng tenant, role, department, ACL; caller không thể override bằng tool arguments. |
| Phân tách approval signer và workload secrets | `REVIEW_SECURITY, GUARDRAILS & HITL.md` (4.3/4.6) | **IMPLEMENTED (code + manifest + regression), chờ probing staging** — Supervisor/API giữ Ed25519 private key; L3/L4 chỉ giữ public key và attenuate/verify capability. ExternalSecret tách riêng API / Chat / RAG / Coding / MCP / maintenance / database; production role verifier fail startup nếu nhận private key. |
| Ngân sách token cứng theo tenant + cảnh báo đột biến | `REVIEW_FINOPS & COST GOVERNANCE.md` (13.1, 13.5) | **CONFIGURED — cần kiểm chứng runtime**. Cost-tag `client_id/department/env` đã phát metric `agent_cost_allocation_total`. |
| WAL PITR với RPO = 0 | `REVIEW_SRE & DISASTER RECOVERY.md` (10.2) | **PARTIAL / honest gap** — chỉ có logical backup CronJob (`deploy/k8s/backup.yaml`). PITR RPO=0 cần WAL archiving + S3/GCS do nhà cung cấp DB quản lý. |

### 6.2.1 Bản Ghi Đợt Gia Cố P0 (2026-11-24)

Đợt gia cố đã xử lý các blocker Critical (có regression test trong `tests/unit/test_*_v3.py`):
ENVIRONMENT fail-open; HITL grant forgeable + wildcard owner + replay; xác minh grant một lần tại
ranh giới MCP và ràng buộc exact arguments; nhánh `approval_granted` fail-open; SSE/A2A output chưa
sanitize; guardrail double-encoding; tenant/RBAC override qua tool argument; cô lập LTM theo tenant;
RTBF mở rộng qua vector/KG/cache/raw files; cache invalidation không xóa L2; symlink escape trong repo
tool; rate limiter production fail-open; MCP probe/pgBouncer/Ingress/OTLP blockers; release gate không
nằm trong CI; sign-off gate chỉ kiểm tra file tồn tại; action/base image chưa pin immutable digest.

Kết quả code-level tại workspace ngày 2026-11-25 (sau đợt hardening v6): Ruff (`0` lỗi) và mypy strict (`84` file nguồn) đạt; `432 passed, 1 skipped` với coverage `90.72%`; `kubectl kustomize deploy/k8s`, `uv lock --check`, `pip-audit` (production lockfile), `red_teaming_runner.py` (`100%`) và `run_eval.py --mode smoke` đều đạt (bổ sung 12 regression tests tại `tests/unit/test_production_hardening_v6.py`). Strict release gate vẫn **FAIL** vì không có LiteLLM/LLM judge thật đang reachable trong môi trường kiểm định này.

### 6.2.2 Bản Ghi Đợt Gia Cố PRR Toàn Diện P0/P1/P2 (2026-11-26)

Đã triển khai và kiểm chứng mức code/manifest/eval cục bộ cho toàn bộ các hạng mục P0, P1, P2 khả thi trong kho mã nguồn (bổ sung regression & E2E tại `tests/integration/test_prr_remediations.py`):
- **P0-1 (Tenant Propagation & RTBF Separation)**: Truyền `tenant_id` từ JWT xuyên suốt `get_current_user` $\rightarrow$ `AgentState.metadata` $\rightarrow$ metadata A2A; bỏ fallback `owner_id` trong `_authoritative_tenant_id()`; tách xóa RTBF theo `owner_id == user_id` khỏi `purge_tenant_corpus=True`.
- **P0-2 (Stateless Streamable-HTTP MCP)**: Chuyển các MCP server sang `MCPServer(..., stateless_http=True, json_response=True)` qua `/mcp` + `streamable_http_client`, cấu hình `sessionAffinity: ClientIP` trong K8s Service và bổ sung contract test với `uvicorn` MCP thật.
- **P0-3 (CI Image Path Alignment)**: Sửa workflow CI thay cả image `newName: ghcr.io/${GITHUB_REPOSITORY,,}/...` lẫn tag `sha-${GITHUB_SHA}`.
- **P1/P2 Security, Persistence & Infra**: Idempotency key A2A + chỉ retry SSE trước frame đầu tiên; phê duyệt HITL 2 pha ở mức tool-call (`code_exec_bash`, `repo_write_file`) kèm kiểm tra `tenant_id` & four-eyes ở production; đánh giá rủi ro theo từng tool (default-deny cho tool chưa khai báo) + bền vững hot-reload YAML; OIDC/JWKS + JTI revocation; `s2s_accepted_tokens`; cắt stdout/stderr sandbox 32 KiB + chặn đọc `data/index`, `data/raw` + chống ReDoS; K8s `agentic-sandbox` PSA `restricted` / `ResourceQuota` / `LimitRange`; `AsyncConnectionPool` + `AsyncPostgresSaver` kèm probe `/readyz`; tách `postgres_app_uri`/`postgres_migration_uri` và `redis-state` (`noeviction`) / `redis-cache` (`allkeys-lru`, tùy chọn fallback local có giới hạn); `audit_logs` hash chain + `pg_advisory_xact_lock` + trigger bất biến + `REVOKE UPDATE/DELETE/TRUNCATE`; tắt Istio sidecar cho Job/CronJob + mở permissive metrics port + CronJob `agentic-qdrant-backup` + Job `agentic-rag-bootstrap` + `topologySpreadConstraints`/`preStop` + tham chiếu `deploy/infra/docker-compose.stack.yml`; incremental CDC `ingest_single_document` + `/api/v1/admin/rag/cdc`; LTM FTS + `planner.py` recall + `delete_older_than(90)`; LangSmith redaction; sửa `HITL_PENDING_APPROVALS` gauge + bổ sung alerts `CheckpointPersistErrors`/`RateLimitUnavailable`/`RAGRetrievalDegraded`; sửa `evals` (`eval_generator_model`, `expected_risk`, 25/16/20 mẫu).
- **Kết quả chạy lại toàn bộ sau chỉnh sửa (2026-11-27)**: `ruff check` đạt (`All checks passed!`), `mypy src/` đạt (`0` lỗi trên `84` file), `evals/red_teaming_runner.py` đạt `100.0%` (`15/15` attack blocked, `5/5` benign cleared), `evals/run_eval.py --mode smoke` đạt (`Routing 100.0%`, `Risk 96.0%`, `Tools 100.0%`, smoke proxy Groundedness `0.70`, Faithfulness `0.78`), và `pytest tests/ -n auto` đạt **`470 passed, 1 skipped`** (`test_repo_server_does_not_follow_symlink_outside_workspace` bị skip do quyền tạo symlink trên Windows).
- **Khoảng trống hạ tầng thật vẫn còn mở (ngoài phạm vi code-only)**: Chạy `evals/release_gate.py` và mở rộng benchmark lớn với LLM/Qdrant/Embedding staging thật; diễn tập tải K8s + PITR WAL restore drill + tạo role DB thực tế (`agentic_app`/`agentic_migrator`) + cấp OIDC/JWKS & Vault secrets trên môi trường thật; và thu thập đủ 9 chữ ký Release Board (`evidence/signoff/release-board.json`).

### 6.2.3 Bản Ghi Đợt Kiểm Định Kiến Trúc Trưởng & Khắc Phục Toàn Diện (2026-11-27)

Đợt kiểm định thực nghiệm và khắc phục tiếp nối (bổ sung regression tests tại `tests/unit/test_chief_architect_remediations.py`) đã đóng toàn bộ các lỗ hổng phát hiện tại mã nguồn, cấu hình, CSDL và Kubernetes manifests:
- **Database Migrations, Least-Privilege & Bandit Clean**: Thêm Alembic revision `004_audit_logs_seq_and_checkpoints.py` bổ sung `audit_logs.chain_seq` (`BigInteger` Identity + index `idx_audit_logs_chain_seq` cho truy vấn chuỗi bất biến trong `audit.py`) và pre-seed schema `LangGraph PostgresSaver` (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes.task_path`, `checkpoint_migrations` v0..9); cấu hình `path_separator = os` trong `alembic.ini`; tách `postgres_migration_uri` khỏi runtime `postgres_app_uri` và đặt `AUTO_RUN_MIGRATIONS=false` để pod runtime production chỉ gọi read-only `check_database_migrations()` fail-closed thay vì chạy DDL `saver.setup()`. Loại bỏ 4 cảnh báo SQL `Bandit B608` bằng static dict queries tại `long_term.py` và `data_lifecycle.py`, đồng thời đồng bộ `prepare_threshold=None` cho PgBouncer transaction pooling.
- **API Edge Security, Secrets & Caching**: Bắt buộc `checkpoint_hmac_secret` riêng biệt (khác `s2s_internal_token`) cho các role `api`/`maintenance`/`all`; kiểm tra `jti` revocation fail-closed (`HTTP 503` khi `redis-state` gián đoạn ở production) với cache xác thực JWT theo request chạy off-event-loop (`asyncio.to_thread`); `/readyz` kiểm tra cả `redis_url` và `redis_state_url`; `/rag/cdc` dùng `workflow_id` có suffix duy nhất; RTBF fan-out xóa key trên cả `redis-state` lẫn `redis-cache`; `RAGCache`/`PromptCache` ưu tiên `redis_state_url` (noeviction) cho invalidation generation, giải tỏa lock chờ không block event loop bằng `adesynchronize_waiters()`, và tỉa stale leaf nodes khỏi `docstore.json` khi `delete_source`/`ingest_single_document` chạy; cưỡng chế RAG RBAC ở cả `staging` lẫn `production`.
- **Orchestration, A2A & MCP Boundaries**: Chuẩn hóa mặc định `MCP_TRANSPORT=streamable-http` (`/mcp`) xuyên suốt `config.py`, `docker-compose.dev.yml` và `.env.example`; `repo_server` chặn file `.env`/`.env.*`/`.envrc` ở mọi cấp thư mục (trừ `.env.example`); `guardrails.py` quét obfuscated base64 trên cả văn bản đã lọc ZWSP/homoglyph; `a2a/executor.py` bổ sung idempotency qua Redis (`redis-state`) cho cả streaming `execute_stream`; `a2a/client.py` tôn trọng `allow_local_agent_fallback`; `dispatch.py` & `agent_workflow.py` hỗ trợ gom phê duyệt 2 pha tool-call song song tất định và `ToolCallApprovalRequired` non-retryable qua Temporal; Temporal `worker.py` expose probe HTTP `/healthz` & `/readyz` trên cổng `9105` cùng graceful shutdown `SIGTERM`.
- **Kubernetes, Serving & Eval Calibration**: Loại bỏ ngoại lệ `PERMISSIVE` trên Istio (thuần `STRICT` toàn namespace) và khóa `allow-agents-to-mcp-tools` đúng principal/cổng từng sub-agent (loại trừ `rag`); đặt `RATE_LIMIT_DEGRADED_LOCAL_FALLBACK="false"` trong `configmap-secrets.yaml`; tách PVC `agentic-qdrant-backups` (streaming SHA-256 + offsite upload fail-closed + dọn 14 ngày) và PVC RWX `agentic-mcp-scratch` (`deploy/k8s/mcp-scratch-storage.yaml`); bổ sung `HF_HOME=/opt/hf-cache` & `HF_HUB_OFFLINE=1` cho `temporal-worker` và `rag-bootstrap-job`; chuẩn hóa `configs/litellm_config.yaml` (`BAAI/bge-m3`, `drop_params: true`), `evals/datasets/golden_v1.jsonl` (`tc_03` -> `expected_risk: "high"`), và hiệu chỉnh mô tả thực tế trên `AGENTS.md`, `Architecture.md`, `configs/prompts/*.md` và `.workflows/specifications/*.md`.
- **Trạng thái Go-Live Production**: Bằng chứng mức mã nguồn được sinh tại [evidence/prr/latest.json](../../evidence/prr/latest.json) với verdict `AUTO-EVIDENCE PENDING REVIEW BOARD`. Cổng Go-Live thật vẫn **CHƯA ĐẠT** do môi trường cục bộ chưa vận hành cụm staging/production thật để vượt qua `evals/release_gate.py`, chưa diễn tập tải K8s / chaos / WAL PITR `RPO=0` trên hạ tầng thật, và chưa có 9 chữ ký Release Board (`evidence/signoff/release-board.json`).

### 6.2.4 Bản Ghi Đợt Khắc Phục Toàn Diện Hậu-Review P0 / P1 / P2 (2026-11-27)

Đợt rà soát đối chiếu thực nghiệm mã nguồn và khắc phục bổ sung (kèm regression suite tại `tests/integration/test_review_remediations_p0_p2.py`) đã đóng toàn bộ các lỗ hổng P0, P1, P2 ở cấp mã nguồn, cấu hình, manifest và tài liệu:
- **P0-1..4 (Tenant Isolation, Multi-Turn Reset, Staging Inviolables & Real PRR Evidence)**: Băm `tenant_id`, `owner_id`, `thread_id`, `turn_id` và `step_fingerprint` vào `idempotency_key` ở cả `hitl.py`/`dispatch.py`/`client.py` lẫn `_scoped_idempotency_key` tại `executor.py` (`execute` & `execute_stream`); thêm `last_turn_human_message_id` và `build_turn_input()` reset toàn bộ trạng thái per-turn ở mỗi lượt mới (`state.py`, `chat.py`, `planner.py`); đặt `require_authentication: true` fail-closed mặc định, áp toàn bộ production inviolables cho cả `staging` qua `is_strict_environment` / `is_strict_env`, chỉ giữ hardcoded tokens cho `environment == "test"`, và bổ sung đầy đủ env staging cho `release-gate` trong `ci.yml`; mở rộng bộ benchmark lên **100** routing (`golden_v1.jsonl`), **50** RAG QA (`rag_qa.jsonl`), **60** red-teaming (`red_teaming.jsonl`), sửa chấm điểm câu từ chối rỗng trong `evaluators.py`, gắn nhãn `planner_mode` + fail release khi planner degraded (`run_eval.py`), và nâng cấp `scripts/generate_prr_evidence.py` chạy `pytest` đầy đủ kèm `--cov=src --cov-fail-under=85` và `restore_drill --smoke`.
- **P1 (HITL, Two-Phase ToolNode, Semantic Cache, Observability, K8s & Deps)**: Chống rò gauge `HITL_PENDING_APPROVALS` khi LangGraph replay node (`_PENDING_APPROVAL_KEYS`); mở rộng HITL 2 pha theo `policy.is_approval_required` gồm `code_exec_python`, `code_exec_bash` và `repo_write_file` qua `ToolNode` giữ nguyên exception (`agents/base.py`); giới hạn leo thang rủi ro theo từ khóa ở `_apply_risk_policy` tránh approval fatigue cho `chat`/`rag`; lazy-resolve `LlamaSettings.embed_model` trong `RAGCache` + chặn fuzzy trigram ở `staging`/`production`/`require_embedding_service` + quantifier guard + chia sẻ cache theo tenant/dept/role (tự hạ `private_scope` khi gặp `owner_id`-only ACL) + redact đồng nhất bảo toàn `[CITATIONS]`; cô lập ingestion theo `data/raw/tenants/<id>/` + `rag_include_public_corpus`; đo fallback ngầm phía LiteLLM server qua `agent_model_proxy_fallback_total` trong `gateway.py` & alert `ModelCircuitBreakerTripped`; sửa `llamacpp-chat.sh` (`--jinja`, bỏ `--embedding false`) & `llamacpp-embed.sh` (`--embedding`); ghi `actor_id` admin thực thi RTBF và bổ sung `verify_audit_chain` (`scripts/verify_audit_chain.py`); bổ sung egress Temporal `7233` & JWKS `443`, `storageClassName: efs-sc`, `deploy/k8s/serving.yaml` & `deploy/k8s/datastores.yaml`, khai báo `httpx2>=2.0.0` (bắt buộc bởi `mcp==2.3.0`) và siết lower bounds trong `pyproject.toml` + `uv.lock`, đồng thời thêm smoke check container Python vào `ci.yml`.
- **P2 (QA Cleanup, DR Drill, SLOs/Fast-Path, Key Rotation & Docs Sync)**: Thay các assertion `>= 0` yếu trong test coverage; bảo vệ `seed_eval.py` bằng cờ `--force`; thêm `scripts/restore_drill.py`; bổ sung conversational fast-path (`enable_chat_fast_path` / `fast_finalize_node`) và [docs/slos.md](../../docs/slos.md); chỉnh `mask_pii` chạy API key trước phone (tránh ăn nhầm `sk-...`), hỗ trợ SĐT VN có khoảng trắng (`0912 345 678`) và mở rộng `TOXICITY_PATTERNS`; hỗ trợ xoay vòng khóa `kid`/`approval_previous_public_keys` và `checkpoint_hmac_previous_secrets`; đổi Argo `PreSync` sang Sync Waves (`db-migration-job.yaml`), tắt CORS tầng Ingress (`enable-cors: "false"`) + thêm `deploy/overlays/production/kustomization.yaml`, và đồng bộ toàn bộ tài liệu kiến trúc/runbooks/specs/PRR.
- **Kết quả kiểm chứng máy móc sau đợt khắc phục**: Các cổng code-level đạt `exit=0`: `ruff check .`; `mypy --no-incremental src/ evals/ scripts/` trên **102 files**; `uv lock --check`; Bandit mức High không có finding; `pip-audit` trên production lock graph không có lỗ hổng đã biết; render `kubectl kustomize deploy/overlays/production` thành công **114 documents**; full suite **515 passed, 1 skipped** (Windows symlink `WinError 1314`). Coverage đo từ `coverage.json`: line **91.24%** (ngưỡng 85%), branch **80.28%** (ngưỡng 80%), `src/agentic/core` line **97.28%** (ngưỡng 95%). `red_teaming_runner.py` đạt **100.0%** (45/45 attack, 15/15 benign) và `restore_drill.py --smoke` đạt (`checksum_verified=true`, `audit_valid=true`, 1 record). Smoke eval hoàn tất `exit=0` trong **22.300 giây** trên 100 routing + 50 RAG cases. Strict gate fail-fast trong **2.634 giây**, `exit=1`, với blocker ngoại vi đo được: **LLM gateway unreachable — no real generation or LLM judging possible**. Các đầu việc phụ thuộc cụm thật (`release_gate.py` trên vLLM/BGE-M3/Qdrant, Locust/chaos/PITR restore drill, Secret/Ingress cụm đích và 9 chữ ký Release Board) vẫn là cổng bắt buộc trước Go-live.

### 6.2.5 Khắc Phục Blocker Source Của Release Gate (2026-11-27)

- Smoke eval dùng retrieval async và ép reranker heuristic nội bộ, không tải CrossEncoder; strict environment vẫn fail-closed nếu cố tắt neural reranker.
- Mỗi sample và mỗi release stage có deadline cấu hình; timeout/exception được báo thành FAIL ổn định thay vì treo hoặc crash không có summary.
- DeepEval trở thành dependency bắt buộc của strict gate; package `evals` chuyển sang lazy exports để prerequisite probe không import Torch/LlamaIndex/RAGAS/DeepEval trước khi cần.
- CI dùng `uv sync --frozen`/`uv run --no-sync` cho lint, test và release; PRR generator trả non-zero khi có check fail/timeout, artifact vẫn được upload để chẩn đoán, và sign-off từ chối `all_checks_passed != true` hoặc bất kỳ check thất bại nào.

> **CẦN HIỂU RÕ**: đây là bằng chứng **code-level**. Nó KHÔNG thay cho việc chạy
> `python evals/release_gate.py` với LLM judge/neural model thật, diễn tập DR/load test trên
> cụm K8s thật, và 9 chữ ký của Release Board. Verdict tự động vẫn là
> `AUTO-EVIDENCE PENDING REVIEW BOARD`.

### 6.3 Bảng Chữ Ký Phê Chuẩn Của Hội Đồng Đánh Giá (Release Board)

> **9 ô chữ ký dưới đây để trống — chờ ký bởi Release Board.** Không được tự động điền tên, ngày hay "PASSED".

| # | Vai Trò (RACI) | Đại Diện | Quyết Định (Go / Waiver / No-Go) | Chữ Ký | Ngày (UTC) |
| :---: | :--- | :--- | :---: | :---: | :---: |
| 1 | Chief AI Architect (A) | Lead Architect | ____________ | ____________ | ____________ |
| 2 | Lead AI/Backend Engineer (R) | Backend Tech Lead | ____________ | ____________ | ____________ |
| 3 | QA / Test Engineering Lead (R/A) | Head of QA | ____________ | ____________ | ____________ |
| 4 | SecOps / Security Lead (C) | Head of Cyber Security | ____________ | ____________ | ____________ |
| 5 | Legal Counsel & DPO (C/A) | Legal & DPO Lead | ____________ | ____________ | ____________ |
| 6 | FinOps / Cloud Cost Lead (C) | FinOps Lead | ____________ | ____________ | ____________ |
| 7 | Data Governance Lead (R) | Data Governance Lead | ____________ | ____________ | ____________ |
| 8 | Principal SRE / DevOps (R/A) | Principal SRE | ____________ | ____________ | ____________ |
| 9 | Product Owner (I) | Product Manager | ____________ | ____________ | ____________ |

*Chuyển tiếp theo giao thức hai commit: Hội đồng ký source commit `C`; tạo commit attestation `A` chỉ thêm/sửa `evidence/signoff/release-board.json` với `commit_sha=C`; sau đó mới tạo release tag trỏ vào `A`. CI bắt buộc `parent(A)=C`, diff của `A` chỉ có đúng tệp biên bản, và PRR artifact phải thuộc `A`. Nếu có bất kỳ thay đổi code/config nào sau `C`, phải hủy attestation và tái thẩm định từ commit nguồn mới.*
