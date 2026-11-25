# troubleshooting.md — Sổ Tay Xử Lý Lỗi & Bẫy Thường Gặp (Gotchas)

> Tệp này ghi lại các sự cố thực tế, các cạm bẫy đã gặp trong quá trình phát triển dự án và giải pháp đã kiểm chứng.

## MCP streamable HTTP exception bị bọc nhiều tầng

- **Triệu chứng**: integration test mong một `RuntimeError` trực tiếp nhưng AnyIO/Starlette trả về `ExceptionGroup` lồng nhau, với lỗi giao thức thực tế là `MCPError` ở leaf.
- **Nguyên nhân**: task-group của ASGI transport bảo toàn nhiều lỗi đồng thời và bọc exception từ MCP client/server boundary.
- **Khắc phục**: test bắt `ExceptionGroup`, duyệt đệ quy các `BaseExceptionGroup` và xác nhận có `MCPError` leaf; không phụ thuộc vào số tầng bọc của runtime.

## Focused pytest-cov chậm bất thường trên Windows

- **Triệu chứng**: một nhóm test nhỏ chạy nhanh khi không đo coverage nhưng focused run với pytest-cov có thể kéo dài hoặc timeout.
- **Khắc phục**: dùng focused test không coverage để debug nhanh, sau đó luôn chạy full `make test` làm nguồn coverage chính thức; không hạ gate hoặc dùng báo cáo coverage cũ.

## Full test gate vượt timeout dù không có lỗi

- **Triệu chứng**: `make test` bị tiến trình điều phối cắt ở 15 phút mà pytest chưa trả mã thoát.
- **Nguyên nhân đã đo**: `test_run_ragas_benchmark_quality_gate` có thể mất khoảng 530 giây trên Windows; thêm CrossEncoder và teardown làm toàn gate cần gần 17 phút khi bật coverage.
- **Cách xác minh**: tách chạy unit (**492/492**) và integration/e2e (**34/34**) để loại trừ bài treo, sau đó chạy lại đúng `make test` với timeout 25 phút. Kết quả cuối: **526 passed**, coverage **88.42%**.
- **Lưu ý**: timeout của runner không phải PASS. Phải lấy được exit code 0 và báo cáo coverage cuối; CI/local orchestration nên dành ít nhất 25 phút cho gate này.

---

## 1. Lỗi: PgBouncer "Prepared Statement Already Exists" Hoặc "Prepared Statement Does Not Exist"
- **Hiện tượng**: Khi chạy connection pool kết nối tới PostgreSQL thông qua PgBouncer ở chế độ `transaction pooling`, phát sinh lỗi liên quan đến prepared statements.
- **Nguyên nhân**: PgBouncer ở transaction mode chia sẻ kết nối giữa các transaction khác nhau, prepared statement tạo ở connection này không tồn tại hoặc bị trùng lặp ở connection khác.
- **Giải pháp**:
  - Trong `short_term.py` và `long_term.py`, khi khởi tạo `ConnectionPool`, thiết lập `prepare_threshold=None`.
  - Cấu hình PgBouncer `POOL_MODE: transaction` kết hợp `MAX_PREPARED_STATEMENTS: 100`.

---

## 2. Lỗi: Redis Checkpointer Deserialization Security Alert
- **Hiện tượng**: Kiểm tra an ninh cảnh báo rủi ro deserialization giả mạo (tampering) trên Redis cache lưu state của LangGraph.
- **Nguyên nhân**: State checkpoint serialize bằng pickle hoặc JSON có thể bị can thiệp nếu hạ tầng mạng Redis bị xâm nhập.
- **Giải pháp**:
  - Trong `redis_saver.py`, bổ sung lớp xác thực chữ ký số **HMAC-SHA256** trên dữ liệu serialized trước khi `SET` vào Redis.
  - Khi đọc (`GET`), xác thực lại chữ ký HMAC trước khi deserialize. Nếu chữ ký không khớp, từ chối nạp và ghi log security incident.

---

## 3. Lỗi: Event Loop Lagging Khi Query RAG Lớn
- **Hiện tượng**: Khi RAGAgent thực hiện BM25 lexical search hoặc Cross-Encoder reranking trên tập tài liệu lớn, toàn bộ API L1 bị giật lag và response time tăng đột biến.
- **Nguyên nhân**: BM25 và Cross-Encoder chạy đồng bộ trên main thread, chiếm dụng Python GIL và làm nghẽn asyncio event loop.
- **Giải pháp**:
  - Chuyển toàn bộ lời gọi CPU-bound sang worker thread pool bằng `asyncio.to_thread()`.
# Hardening review — 2026-11-23

## LangGraph async API với PostgresSaver sync

- Triệu chứng: `ainvoke`/`astream_events` gọi `aget_tuple` chưa được triển khai.
- Nguyên nhân: graph async được compile bằng `PostgresSaver` đồng bộ.
- Khắc phục: khởi tạo `AsyncPostgresSaver` trong FastAPI lifespan, giữ async context đến shutdown, và dùng `aget_state` ở API.

## Approval không bảo vệ biên tool

- Triệu chứng: planner/HITL có thể duyệt step nhưng MCP tool vẫn bị gọi trực tiếp hoặc một approval mở khóa nhiều step.
- Khắc phục: lưu `approved_step_ids`, truyền capability qua A2A metadata/ContextVar và kiểm tra allowlist + approval tại mọi MCP invocation.

## Thread ID do client cung cấp

- Triệu chứng: hai principal có thể dùng cùng `thread_id` để đọc/ghi chung checkpoint.
- Khắc phục: atomic ownership registry và HMAC-scoped internal checkpoint ID; approval resolve owner từ registry và xác thực đúng pending step.

## CDC path traversal và stale index

- Triệu chứng: `file_path` có thể thoát `data/raw`; `_load_storage()` không index nội dung mới.
- Khắc phục: `resolve/relative_to`, hỗ trợ delete, rebuild persisted index trong worker thread rồi reset retriever singleton.

## Release attestation tự-tham-chiếu SHA

- Triệu chứng: CI yêu cầu `evidence/signoff/release-board.json.commit_sha == github.sha` trong khi chính tệp biên bản phải được commit vào SHA đó; không thể tạo artifact trung thực thỏa điều kiện.
- Nguyên nhân: một Git commit không thể chứa trước SHA của chính nó; đồng thời job publish từng đọc PRR tracked trong checkout thay vì artifact vừa sinh ở workflow hiện tại.
- Khắc phục: dùng giao thức hai commit. Hội đồng ký source commit `C`; commit `A` chỉ chứa biên bản có `commit_sha=C`; tag trỏ vào `A`. CI tải `prr-evidence-<SHA-A>`, xác minh `parent(A)=C` và diff `C..A` chỉ có `evidence/signoff/release-board.json` trước mọi thao tác push.

## Unit test ghi vào SQLite tracked

- Triệu chứng: chạy full suite làm thay đổi `data/memory/long_term_store.db` dù source không đổi.
- Nguyên nhân: một test khởi tạo `LongTermStore(force_sqlite=True)` mà không truyền `storage_dir`, nên dùng đường dẫn project mặc định.
- Khắc phục: mọi unit test SQLite phải dùng `tmp_path`; regression AST quét toàn bộ `tests/unit/test_*.py` và từ chối `force_sqlite=True` thiếu `storage_dir`.

## Circuit breaker test phụ thuộc tốc độ runner

- Triệu chứng: assertion trạng thái `OPEN` thỉnh thoảng thấy `HALF_OPEN` khi chạy xdist, đặc biệt lúc logging chậm.
- Nguyên nhân: test đặt recovery window 100 ms rồi sleep; độ trễ scheduler/logging có thể vượt cửa sổ trước assertion.
- Khắc phục: dùng recovery window 60 giây và dịch `last_failure_time` vượt ngưỡng để kiểm tra transition, không sleep và không patch đồng hồ toàn cục.

## BuildKit không nội suy ARG trong COPY --from

- Triệu chứng: cả ba image fail với `invalid reference format` tại `COPY --from=ghcr.io/astral-sh/uv:${UV_VERSION}`.
- Nguyên nhân: `COPY --from` chỉ nhận tên stage hoặc image reference tĩnh; Dockerfile frontend không mở rộng build ARG tại vị trí này.
- Khắc phục: khai báo `FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv`, sau đó dùng `COPY --from=uv`; regression kiểm tra named stage tồn tại và cấm lại mẫu cũ.

## Pull request bỏ qua container build/Trivy

- Triệu chứng: lint/test/manifest của PR xanh nhưng job `build-and-scan` bị `SKIPPED`; lỗi Dockerfile chỉ xuất hiện sau khi merge/push `main`.
- Nguyên nhân: job có điều kiện `if: github.event_name == 'push'` dù workflow đã nhận sự kiện pull request.
- Khắc phục: bỏ điều kiện push-only và thêm regression bắt buộc Docker build + Trivy fail-closed trên PR; publish/CD vẫn giữ tag-only riêng biệt.

## Trivy chặn image do digest base đã cũ

- Triệu chứng: image build thành công nhưng Trivy báo hàng loạt HIGH/CRITICAL OS CVE, ví dụ `apt 2.6.1`, dù tag Python vẫn thuộc dòng 3.12/bookworm.
- Nguyên nhân: digest bất biến đang trỏ tới snapshot Debian 12.12 cũ; pin digest bảo đảm tái lập nhưng không tự nhận bản vá bảo mật.
- Khắc phục: tra OCI index hiện hành từ registry, cập nhật đồng bộ cả ba Dockerfile sang digest đã rà soát và thêm regression exact-reference. Không dùng `ignore-unfixed`, severity waiver hoặc hạ `exit-code`; CI phải chứng minh Trivy sạch theo policy hiện hữu.

## Debian slim hiện hành vẫn không đạt zero HIGH/CRITICAL

- Triệu chứng: Bookworm 12.15 còn `101` vulnerability (`99 HIGH`, `2 CRITICAL`); Trixie 13.8 còn `53 HIGH`; Distroless Debian 13 còn `35 HIGH` theo cùng Trivy DB.
- Nguyên nhân: image Debian tổng quát vẫn mang package hệ điều hành không cần cho workload và nhiều CVE ở trạng thái `affected`, `fix_deferred` hoặc `will_not_fix`; đổi digest/`apt upgrade` không thể đạt gate zero-CVE.
- Khắc phục: scan candidate trực tiếp trước khi sửa Dockerfile; dùng Chainguard/Wolfi minimal runtime đã pin digest (`0 HIGH/CRITICAL`), dev image riêng cho build, chạy UID 65532 và loại shell/package manager khỏi runtime. Thay `sh -c` bằng Python launcher allowlist + port validation để giữ fail-closed.

## Alembic `StringDataRightTruncation` khi `revision` vượt quá 32 ký tự

- Triệu chứng: Bước CI `Verify Database Migrations (Alembic)` trên PostgreSQL 16 thất bại với `(psycopg.errors.StringDataRightTruncation) value too long for type character varying(32)` tại `UPDATE alembic_version SET version_num='004_audit_logs_seq_and_checkpoints'`, trong khi kiểm thử cục bộ `alembic upgrade head --sql` vẫn xanh.
- Nguyên nhân: Bảng mặc định `alembic_version.version_num` có kiểu `VARCHAR(32)`. ID `004_audit_logs_seq_and_checkpoints` dài `34` ký tự nên vượt giới hạn khi thực thi trên PostgreSQL thật (chế độ `--sql` chỉ in chuỗi SQL nên không phát hiện lỗi kiểu dữ liệu).
- Khắc phục: Rút gọn `revision` trong `migrations/versions/004_audit_logs_seq_and_checkpoints.py` thành `004_audit_seq_checkpoints` (`25` ký tự `<= 32`) và bổ sung unit test hồi quy `test_alembic_revision_ids_fit_alembic_version_and_004_ddl` kiểm tra mọi revision trong `ScriptDirectory` và câu lệnh SQL sinh ra đều có `len <= 32`.

## Production Kustomize overlay tự tham chiếu gây cycle

- Triệu chứng: overlay đặt dưới `deploy/k8s/overlays/production` tham chiếu ngược `deploy/k8s`, khiến Kustomize phát hiện resource cycle hoặc lặp lại chính overlay.
- Nguyên nhân: base chứa thư mục overlay nằm bên trong chính cây được overlay nhập vào.
- Khắc phục: chuyển entrypoint production sang `deploy/overlays/production/kustomization.yaml`, tham chiếu `../../k8s`, rồi bắt buộc CI render đúng entrypoint này. Kết quả đo gần nhất render thành công 114 documents.

## Makefile vô tình dùng Python hệ thống thay vì môi trường `uv`

- Triệu chứng: `make test` chạy bằng Python hệ thống 3.11 dù project khóa Python 3.12 và dependencies nằm trong môi trường `uv`.
- Nguyên nhân: biến `PYTHON`/`PYTEST` mặc định hoặc executable trên `PATH` không đồng nhất với lockfile.
- Khắc phục: cố định `PYTHON := uv run --no-sync python` và `PYTEST := uv run --no-sync pytest`; CI và local dùng cùng lock graph.

## OCI tag khai báo không tồn tại hoặc không bất biến

- Triệu chứng: registry trả `not found` cho tag llama.cpp/LiteLLM cũ; manifest production có thể fail pull hoặc thay đổi nội dung theo tag.
- Nguyên nhân: dùng repository/tag đã đổi tên, bị xóa, hoặc tag mutable mà không khóa digest.
- Khắc phục: xác minh từng image bằng OCI registry trước khi sửa manifest, sau đó pin dạng `tag@sha256:digest` trong cả Kubernetes và Docker Compose. Không coi một tag là hợp lệ nếu registry inspection không trả đúng digest.

## Coverage tổng hợp che khuất branch/core coverage

- Triệu chứng: tổng line coverage vượt ngưỡng nhưng branch coverage hoặc package kernel `src/agentic/core` có thể thấp hơn SLA mà pipeline vẫn xanh.
- Nguyên nhân: chỉ dùng `--cov-fail-under` cho một tỷ lệ tổng, không đọc riêng số nhánh và package trọng yếu.
- Khắc phục: sinh `coverage.json` rồi chạy `scripts/check_coverage_thresholds.py` với ba cổng độc lập: overall line >=85%, overall branch >=80%, core line >=95%.

## Blocking I/O và reverse dependency trong RTBF lifecycle

- Triệu chứng: coroutine RTBF gọi trực tiếp PostgreSQL/Redis/Qdrant/filesystem đồng bộ và tầng `memory` import ngược implementation từ tầng RAG.
- Nguyên nhân: backend được khởi tạo bên trong lifecycle service, vừa block event loop vừa phá Dependency Rule.
- Khắc phục: inject Knowledge Graph store từ caller, fail-closed nếu purge tenant yêu cầu backend nhưng chưa được cấp, và offload toàn bộ I/O đồng bộ qua `asyncio.to_thread()`.

## Smoke eval treo do tải CrossEncoder và gọi retrieval đồng bộ

- Triệu chứng: `run_eval.py --mode smoke` không hoàn tất trong 184.1 giây sau khi cài neural extras.
- Nguyên nhân: smoke mode vẫn tạo `CrossEncoderReranker` neural và gọi `search_policy()` đồng bộ ngay trong event loop; model loader có thể tải weights trước khi fallback.
- Khắc phục: dùng `asearch_policy()` với deadline từng sample và truyền chế độ heuristic nội bộ. `CrossEncoderReranker(use_neural=False)` không tải model, nhưng vẫn raise nếu `require_neural_reranker=True`. Kết quả đo lại: 100 routing + 50 RAG cases đạt `exit=0` trong 22.300 giây.

## Release prerequisite probe chậm do `evals/__init__.py` eager import

- Triệu chứng: gate biết gateway ngoại tuyến nhưng vẫn mất khoảng 16 giây mới báo blocker.
- Nguyên nhân: import một module con của `evals` kích hoạt package `__init__`, kéo theo RAGAS, RAG, orchestrator, LlamaIndex và DeepEval.
- Khắc phục: thay public exports bằng `__getattr__` lazy import và tách probe nhẹ sang `evals/prerequisites.py`; heavy evaluators chỉ import sau khi preflight đạt. Kết quả đo lại: blocker trả về trong 2.634 giây.

## PRR artifact có check fail nhưng pipeline vẫn xanh

- Triệu chứng: `generate_prr_evidence.py` ghi `all_checks_passed=false` nhưng trả exit 0; sign-off chỉ kiểm verdict/commit mà không từ chối check thất bại.
- Nguyên nhân: nhầm lẫn giữa “artifact đã được ghi” và “các phép đo đã đạt”.
- Khắc phục: luôn ghi artifact chẩn đoán nhưng trả exit 1 nếu bất kỳ check fail/timeout; CI upload artifact với `if: always()` và sign-off bắt buộc `all_checks_passed=true` cùng toàn bộ `checks[].passed=true`.

## Lỗi mypy tại `evals/deepeval_runner.py` do lệch optional extra giữa local và CI

- Triệu chứng: CI job `Lint, Type Check & Vulnerability Audit` (cài `--extra dev`, không có `deepeval`) fail tại `evals/deepeval_runner.py:194` với `[unused-ignore]` và `Class cannot subclass "DeepEvalBaseLLM" (has type "Any") [misc]`, dù chạy `mypy src/ evals/ scripts/` ở local (đã cài `--extra deepeval`) vẫn pass.
- Nguyên nhân: Khi cài `deepeval`, `DeepEvalBaseLLM.__init_subclass__` chưa có type annotation nên phát sinh `[no-untyped-call]`; khi không cài `deepeval` trên CI, `ignore_missing_imports = true` biến `DeepEvalBaseLLM` thành `Any` nên phát sinh `[misc]` và làm `# type: ignore[no-untyped-call]` trở thành unused.
- Khắc phục: Dùng `if TYPE_CHECKING:` khai báo lớp cơ sở `_GatewayLLMBase` có type đầy đủ, giữ `_GatewayLLMBase = DeepEvalBaseLLM` ở runtime (`else:`), và `cast(DeepEvalBaseLLM, _GatewayDeepEvalLLM(model_alias))` để mypy pass sạch ở cả hai môi trường.

## Pytest-repeat thất bại do cache mô hình tồn tại xuyên lượt

- Triệu chứng: test reranker pass khi chạy riêng nhưng có thể fail ở lượt thứ hai/ba của `pytest --count=3` vì nhánh tải `sentence_transformers` không còn được thực thi.
- Nguyên nhân: `_model_cache` ở `src.agentic.rag.reranker` là cache cấp process; `pytest-repeat` tái sử dụng cùng process nên dữ liệu từ lượt trước tồn tại sang lượt sau.
- Khắc phục: cô lập test bằng `patch.dict(reranker_module._model_cache, {}, clear=True)` cùng mock `sys.modules`; đo lại toàn bộ unit suite ba lượt với `RuntimeWarning` nâng thành lỗi đạt **1,458 passed, 3 skipped, 0 failed**. Ba skip cùng thuộc test tạo symlink trên Windows; sau khi bổ sung junction fallback, chính test đó đạt **3/3** và full CI-style suite đạt **521 passed, 0 skipped**.

## Mock `asyncio.wait_for` làm rò coroutine trong test polling

- Triệu chứng: full suite xanh nhưng phát sinh `RuntimeWarning: coroutine 'Event.wait' was never awaited`; khi dùng `-W error::RuntimeWarning`, cảnh báo làm mất tính tin cậy của gate.
- Nguyên nhân: test thay `asyncio.wait_for` bằng `AsyncMock`; đối số coroutine `stop.wait()` đã được tạo nhưng mock không await hoặc đóng coroutine đó.
- Khắc phục: dùng implementation thật của `asyncio.wait_for`; `Event` đã được set nên lời gọi kết thúc ngay, không làm chậm test và không rò coroutine. Targeted repeat và full stability run đều không còn cảnh báo.

## Gitleaks false-positive tái xuất hiện sau khi squash lịch sử Git

- Triệu chứng: GitHub Actions fail ngay tại `Secret Scan (Gitleaks, full history, fail closed)` dù các fixture test đã được review và từng có fingerprint trong `.gitleaksignore`.
- Nguyên nhân: fingerprint của Gitleaks chứa commit SHA. Sau khi squash thành commit mới, cùng fixture nhận SHA/line mới nên exception cũ không còn khớp; run `46149979723` phát hiện hai chuỗi `jwt_secret_key` trong `test_chief_architect_remediations.py`.
- Khắc phục: xác minh từng phát hiện là dữ liệu test, thêm exception theo đúng fingerprint của commit lịch sử (không allowlist theo path/rule), đồng thời ghép chuỗi fixture ở HEAD để lần squash sau không tạo lại detector match. Đo lại bằng chính Gitleaks `8.30.1`: 22 commit, `no leaks found`.
