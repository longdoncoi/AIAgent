# REVIEW_TESTING & QA STRATEGY.md — Trụ Cột 11: Chiến Lược Kiểm Thử Toàn Diện & Đảm Bảo Chất Lượng Phần Mềm

> **Tài liệu kiểm định chuyên biệt (Production Readiness Review - Pillar 11 / 14)**  
> **Đơn vị phụ trách**: QA Lead & Principal Test Engineer & Backend Tech Lead  
> **Tài liệu tham chiếu**: [REVIEW.md](REVIEW.md) · [Architecture.md](../../Architecture.md) · [tests/](../../tests) · [tests/load/locustfile.py](../../tests/load/locustfile.py) · [tests/integration/test_chaos_resilience.py](../../tests/integration/test_chaos_resilience.py)

---

## 1. Mục Tiêu & Phạm Vi Kiểm Định

Trụ cột này bảo đảm toàn bộ hệ sinh thái mã nguồn của `agentic-platform` đáp ứng chuẩn mực kiểm thử phần mềm cấp doanh nghiệp:
1. **Kim Tự Tháp Kiểm Thử (Test Pyramid)**: Cân đối hoàn hảo giữa Unit Tests (nhanh, cô lập), Integration Tests (giao tiếp A2A, MCP, CSDL), Contract Tests (tương thích schema), và End-to-End Tests (SSE Streaming, HITL).
2. **Định Mức Bao Phủ Mã Nguồn (Code Coverage SLA)**:
   - Kernel `src/agentic/core/`: $\ge 95\%$ Line Coverage.
   - Orchestrator & Agents: $\ge 85\%$ Line Coverage.
   - Caching, Safety & Security: $\ge 90\%$ Line Coverage.
   - Toàn bộ dự án: $\ge 85\%$ Line Coverage, $\ge 80\%$ Branch Coverage.
3. **Kiểm Thử Kháng Sự Cố & Hỗn Loạn (Chaos Engineering & Resilience)**: Đảm bảo hệ thống tự phục hồi khi Redis sập (fallback xuống L1 memory), PostgreSQL mất kết nối tạm thời (SQLite dead-letter fallback), hoặc vLLM GPU sập (chuyển sang llama.cpp CPU).
4. **Kiểm Thử Hiệu Năng & Chịu Tải (Load & Stress Testing)**: Thử nghiệm tải đồng thời bằng Locust với 100+ concurrent users, xác nhận không rò rỉ socket, không cạn kiệt connection pool và P99 latency $< 4.5$ giây.
5. **Vệ Sinh Mocking & Quản Lý Flaky Tests**: Không mock các module nghiệp vụ cần đo lường, không phụ thuộc kết nối internet công cộng khi chạy CI, cách ly triệt để các bài test chập chờn.

---

## 2. Danh Mục Checklist Kiểm Tra Chi Tiết

| ID | Trạng Thái | Mức Độ | Tiêu Chí Kiểm Tra (Verification Criteria) | Tệp Mã Nguồn / Cấu Hình | Phương Pháp & Lệnh Kiểm Chứng |
| :---: | :---: | :---: | :--- | :--- | :--- |
| **11.1** | `[ ]` | **P0** | **Độ Bao Phủ Mã Nguồn (Code Coverage Gate)**: Toàn bộ dự án đạt $\ge 85\%$ Line Coverage và $\ge 80\%$ Branch Coverage. Module `core/` đạt $\ge 95\%$. Không có untested regression trong các bản commit mới. | [pyproject.toml](../../pyproject.toml)<br>`tests/` | Chạy đo lường coverage:<br>`pytest tests/ -v --cov=src --cov-report=term-missing`<br>*(Yêu cầu: Tổng số pass $\ge 110$, Coverage $\ge 85\%$)* |
| **11.2** | `[ ]` | **P0** | **Phân Tầng Kim Tự Tháp Kiểm Thử (Test Pyramid)**: Có đầy đủ 4 tầng kiểm thử riêng biệt: Unit (`tests/unit/`), Integration (`tests/integration/`), E2E (`tests/e2e/`), và Load (`tests/load/`). Các tầng độc lập, chạy được riêng lẻ. | [tests/](../../tests) | Kiểm tra thực thi riêng từng thư mục:<br>`pytest tests/unit/ -q`<br>`pytest tests/integration/ -q`<br>`pytest tests/e2e/ -q` |
| **11.3** | `[ ]` | **P0** | **Kiểm Thử Kháng Sự Cố & Hỗn Loạn (Chaos Engineering)**: Đã kiểm thử thành công các kịch bản: (1) Sập Redis $\rightarrow$ chuyển sang L1 In-Memory; (2) vLLM GPU lỗi $\rightarrow$ Circuit Breaker kích hoạt sang llama.cpp CPU; (3) Temporal worker retry. | [tests/integration/test_chaos_resilience.py](../../tests/integration/test_chaos_resilience.py) | Chạy chaos resilience test suite:<br>`pytest tests/integration/test_chaos_resilience.py -v` |
| **11.4** | `[ ]` | **P0** | **Kiểm Thử Tải & Chống Rò Rỉ Tài Nguyên (Load & Soak Testing)**: Kịch bản Locust mô phỏng 100 người dùng đồng thời trong tối thiểu 5 phút; kiểm tra không có lỗi `Too many open files`, tỷ lệ lỗi $< 0.1\%$, P95 $< 2.5s$, P99 $< 4.5s$. | [tests/load/locustfile.py](../../tests/load/locustfile.py) | Chạy Locust ở chế độ headless:<br>`locust -f tests/load/locustfile.py --headless -u 100 -r 10 -t 5m --host http://localhost:8000` |
| **11.5** | `[ ]` | **P1** | **Kiểm Thử Hợp Đồng Giao Thức (Contract / Schema Testing)**: Toàn bộ gói tin A2A Task và MCP Tool arguments được validate bằng Pydantic v2. Mọi trường hợp gửi sai schema phải trả về lỗi `422 Unprocessable Entity` có cấu trúc. | `src/agentic/core/schemas.py`<br>[src/agentic/a2a/cards.py](../../src/agentic/a2a/cards.py) | Chạy unit test schema validation:<br>`pytest tests/unit/test_core.py -v -k "test_schemas or test_contract"` |
| **11.6** | `[ ]` | **P1** | **Cách Ly Môi Trường & Triệt Tiêu Flaky Tests**: Không có bài test nào phụ thuộc mạng internet ngoài. Mọi external API (LiteLLM, vLLM) được cấu hình mock rõ ràng hoặc dùng test fixture cục bộ. Flaky rate $= 0\%$. | [tests/conftest.py](../../tests/conftest.py) | Chạy test lặp lại 3 lần liên tiếp:<br>`pytest tests/unit/ --count=3 -q` |

---

## 3. Quy Trình Xác Minh & Lệnh Thực Thi (Pre-Flight Runbook)

### Bước 1: Kiểm Tra Toàn Diện Test Suite & Báo Cáo Coverage
```bash
# Sử dụng virtualenv của dự án
.venv\Scripts\pytest.exe tests/ -v --cov=src --cov-report=term-missing --cov-fail-under=85
```
*Tiêu chí đạt*: 100% test cases Pass (tối thiểu 110 tests), coverage $\ge 85\%$.

### Bước 2: Kiểm Tra Tải Đồng Thời Bằng Locust
```bash
# Chạy giả lập 100 concurrent users với ramp-up 10 users/giây trong 5 phút
locust -f tests/load/locustfile.py --headless -u 100 -r 10 -t 5m --host http://localhost:8000
```
*Tiêu chí đạt*:
- Tỷ lệ lỗi (Error Rate): $< 0.1\%$
- P95 Response Time: $< 2500$ ms
- P99 Response Time: $< 4500$ ms
- Không có exception liên quan tới connection pool exhaustion.

### Bước 3: Diễn Tập Kháng Lỗi Hỗn Loạn (Chaos Verification)
```bash
.venv\Scripts\pytest.exe tests/integration/test_chaos_resilience.py -v
```
*Tiêu chí đạt*:
- `test_circuit_breaker_failover_routing`: Pass.
- `test_redis_outage_graceful_degradation`: Pass.

---

## 4. Danh Mục Giám Sát Sau Triển Khai (Post-Deployment Hypercare)

Sau khi hệ thống vận hành chính thức:
- [ ] **T+1 Giờ**: Đối soát tỷ lệ lỗi thực tế trên Production so với kết quả Load Test ($< 0.05\%$).
- [ ] **T+24 Giờ**: Giám sát độ trễ phản hồi P95/P99 thực tế của người dùng, xác nhận nằm trong phạm vi SLA kiểm thử (P95 $< 2.5s$, P99 $< 4.5s$).
- [ ] **T+7 Ngày**: Tổng hợp các edge cases và lỗi phát sinh trong tuần đầu tiên để bổ sung test cases vào bộ Regression Test Suite.

---

## 5. Trạng Thái Nghiệm Thu & Liên Kết Ký Duyệt

- **Tổng số tiêu chí kiểm định**: 6 (P0: 4, P1: 2, P2: 0)
- **Đạt**: _____ / 6
- **Kết luận**: [ ] ĐẠT (PASS) &nbsp;&nbsp;&nbsp; [ ] CẦN KHẮC PHỤC (REJECT)

> 🎯 **Biên Bản Ký Duyệt**: Kết quả đánh giá của Trụ cột này được tổng hợp và ký duyệt chính thức tại [Bảng Chữ Ký Phê Chuẩn Của Hội Đồng Đánh Giá (Mục 6.3 - REVIEW.md)](REVIEW.md#63-bảng-chữ-ký-phê-chuẩn-của-hội-đồng-đánh-giá).

---

## 6. Bằng Chứng Kiểm Định Tự Động (Auto-Evidence Status)

> **AUTO-EVIDENCE PENDING** — chờ đối soát và **ký bởi Release Board**. Không có nhãn `PASSED` được tự động gán cho tài liệu này.

- Artifact: [evidence/prr/latest.json](../../evidence/prr/latest.json) (`commit_sha`, `timestamp_utc`, exit code của `ruff` / `mypy --no-incremental` / `pytest -n auto --cov=src --cov-fail-under=85` / `eval --mode smoke` / `red_teaming_runner` / `restore_drill --smoke` / parse YAML).
- Sinh artifact: `python scripts/generate_prr_evidence.py`. Tất cả ô trạng thái `[]` ở các mục trên vẫn giữ nguyên cho đến khi Hội đồng đối soát artifact và ký duyệt.
- Bảng chữ ký 9 đại diện: [REVIEW.md mục 6.3](REVIEW.md#63-bảng-chữ-ký-phê-chuẩn-của-hội-đồng-đánh-giá).
