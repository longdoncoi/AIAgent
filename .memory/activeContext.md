# activeContext.md — Trạng thái hoạt động hiện tại

## Trọng tâm

- Đợt hardening production-readiness ngày 2026-11-25 đã hoàn tất ở mức source code, manifest và release workflow cục bộ.
- Đánh giá tiếp tục fail-closed: source-level PASS không đồng nghĩa Go-Live; strict AI release gate, staging drills và Release Board vẫn bắt buộc.

## Đã hoàn thành

- HITL step capability được Supervisor/API ký Ed25519; L3 chỉ có public key và attenuate thành call capability exact-argument, one-time; L4 MCP xác minh toàn bộ parent binding, TTL, tool subset và nonce chống replay.
- Production `SERVICE_ROLE` bắt buộc API có private key, agent/MCP có public key và từ chối khởi động nếu private key bị cấp cho verifier/maintenance.
- ExternalSecret tách riêng API, Chat, RAG, Coding, MCP, maintenance và database. Chat/Coding không nhận Qdrant/Neo4j credential; chỉ API nhận approval private key.
- JWT roles/department/client được pin qua API → signed A2A metadata → execution context; RAG/Qdrant/KG áp dụng tenant, role, department và ACL, chặn caller override.
- Long-term memory và Knowledge Graph có namespace tenant; Finalize từ chối RAG thiếu citation đã xác minh.
- Right-to-be-Forgotten fail-closed theo owner, fan-out qua PostgreSQL, Redis, LTM, Qdrant, KG, cache và raw files theo tenant.
- Production rate limiter fail-closed khi Redis unavailable; A2A có `/metrics`; stream latency chỉ ghi khi stream thực sự kết thúc.
- BM25 dùng `rank_bm25`; production bắt buộc neural reranker và embedding service thật.
- Repo MCP chặn symlink/junction escape; server-side tool authorization có regression test.
- Execution context chung nằm ở `core/`, loại bỏ reverse dependency giữa memory/LLM và tools.
- FinOps metadata lấy từ verified context, chống caller override, ghi LiteLLM spend logs và Prometheus mà không dùng user ID làm metric label.
- Redis checkpoint có TTL; PostgreSQL có migration `002_checkpoint_retention`, cleanup parameterized, admin/CLI và CronJob 30 ngày với NetworkPolicy riêng.
- Kubernetes least-privilege routes tách Chat→Web và Coding→Repo/CodeExec; sửa label/cổng pgBouncer; Kustomize dùng labels transformer mới.
- CI action/runtime images được pin; production audit dùng dependency graph có hash từ `uv.lock`, `--no-deps --disable-pip`, tránh resolver lần hai trên mutable index.
- E2E API và integration orchestrator dùng deterministic boundary doubles; full suite không còn phụ thuộc LiteLLM/A2A thật.
- `validate-deploy` cài và chạy toàn bộ parser/test qua cùng môi trường `uv sync --frozen`; regression test chặn việc gọi Python hệ thống thiếu dependency.
- Tất cả GitHub Actions đã nâng lên phiên bản Node 24 hiện hành và vẫn pin bằng commit SHA bất biến; loại bỏ cảnh báo runner cưỡng bức nâng Node 20.
- Publish/CD chỉ chạy cho release tag sau strict gate; tải PRR artifact của đúng workflow/commit thay vì tin bản tracked có thể cũ.
- Release sign-off dùng giao thức hai commit không tự-tham-chiếu: Hội đồng ký source commit `C`; tag trỏ commit attestation `A` chỉ chứa `release-board.json`; CI xác minh `A` có đúng một parent `C` và không có path thay đổi khác.
- Dockerfiles dùng named stage `uv` cho image UV đã pin digest; không nội suy ARG trong `COPY --from`, vốn bị BuildKit từ chối.

## Xác minh mới nhất

- Current committed HEAD là `259c7b352f713ed23959ffd1c8d055e6e23fb6d2`; Dockerfile fix đang ở nhánh `codex/go-live-container-fix`. `evidence/prr/latest.json` tracked không được coi là attestation cho working tree; CI phải tái sinh artifact sau commit.
- `ruff check .`: PASS.
- `mypy src/` strict: PASS, 82 source files.
- Full CI-equivalent suite: `383 passed, 1 skipped in 212.85s`; skip là kiểm thử symlink đặc quyền trên Windows.
- Coverage: `87.70%` (ngưỡng CI 80%).
- Deploy/release regressions gồm locked runtime, tag-only publish, artifact binding, single-parent attestation, Node 24 immutable pins và chặn unit test ghi SQLite vào storage tracked.
- Circuit-breaker state-machine test không còn dùng sleep/cửa sổ 100 ms; timeout được mô phỏng bằng timestamp để ổn định dưới xdist/runner chậm.
- Security/deploy regression cho Ed25519, attenuation, key isolation: PASS.
- YAML parse và `kubectl kustomize deploy/k8s`: PASS.
- `uv lock --check`: PASS.
- Production-only `pip-audit` theo lockfile: không phát hiện vulnerability đã biết.
- GitHub run `45511577877` tại HEAD hiện tại: lint/audit, test/eval và validate-deploy PASS; cả 3 container build FAIL do BuildKit không hỗ trợ `${UV_VERSION}` trực tiếp trong `COPY --from`. Named-stage fix đã có regression cục bộ; cần PR CI xanh để xác nhận build/Trivy thật.
- GitHub runtime audit ngày 2026-11-25: repo/staging/production đều có 0 secret và 0 variable; `production` chỉ có 1 required reviewer, `staging` không có reviewer; `main` không có branch protection khả dụng.
- Runtime preflight ngày 2026-11-24: Docker CLI có nhưng daemon không chạy; `kubectl` không có current context/cluster; không có service production lắng nghe trên các cổng phụ thuộc; các biến kết nối/secret production đều vắng mặt.
- Strict `python evals/release_gate.py`: FAIL đúng thiết kế với `LLM gateway unreachable — no real generation or LLM judging possible`.
- `evidence/signoff/release-board.json`: MISSING; chưa có 9 chữ ký gắn với commit/tag.

## Blocker Go-Live còn lại

1. Provision LiteLLM, embedding service, Qdrant, Redis, PostgreSQL/pgBouncer và OTel/Tempo trên staging; chạy strict release eval với neural stack + RAGAS/DeepEval thật.
2. Chạy load/soak 100 users ≥5 phút, chaos, cross-tenant security probing, cross-pod approval-key probing, RTBF đối soát từng backend và migration/retention drill trên cluster thật.
3. Cấu hình WAL archiving/off-cluster PITR và diễn tập restore; logical PVC backup hiện không đạt tiêu chí RPO=0.
4. Xác thực ExternalSecret/Vault, Alertmanager/Slack, LiteLLM budget rejection và spend-log allocation trên runtime thật.
5. Cấu hình GitHub secrets/variables, đủ reviewer độc lập, branch protection; commit/push các sửa CI và yêu cầu mọi remote job xanh.
6. Thu đủ 9 chữ ký Release Board theo giao thức `C → A → tag`; không tự tạo sign-off artifact.

## Kết luận hiện tại

- Source code + manifest + release workflow gates: PASS tại workspace; remote CI còn chờ commit/rerun.
- Production Go-Live 10/10: CHƯA ĐƯỢC PHÊ DUYỆT do thiếu bằng chứng staging/DR/load/eval/sign-off; không hạ ngưỡng hoặc dùng heuristic thay thế.
