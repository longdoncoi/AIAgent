# AGENTS_OBSERVABILITY_EVALS.md — Quan Sát & Đánh Giá Chất Lượng (Observability & Evals - L9)

> **Tài liệu tham chiếu chuyên sâu** thuộc phân vùng kiến trúc Multi-Agent trong hệ sinh thái `agentic-platform`.  
> Tài liệu gốc: [AGENTS.md](../../AGENTS.md) · [Architecture.md](../../Architecture.md).

---

## 1. Truy Vết Phân Tán & LangSmith Observability (`tracing.py`, `langsmith_tracer.py`)

- Sử dụng OpenTelemetry SDK truyền trace qua Jaeger và tích hợp toàn diện **LangSmith** (`LANGCHAIN_TRACING_V2=true`).
- Hỗ trợ decorator `@traceable_operation`, `LangChainTracer` callback, RunTree context manager, quản lý dataset (`sync_evaluation_dataset`) và ghi nhận phản hồi/chấm điểm LLM trực tiếp vào LangSmith (`log_feedback`). Mọi payload gửi lên LangSmith qua `trace_langsmith_run` và `sync_evaluation_dataset` đều đi qua `redact_trace_payload` để ẩn PII và secrets.
- Mọi A2A RPC request và MCP Tool call đều mang header `traceparent` đồng nhất, liên kết OpenTelemetry trace ID với LangSmith metadata.

---

## 2. Hệ Thống Đo Lường, Prometheus, SLO & Cảnh Báo (`metrics.py`, `docs/slos.md`, `deploy/monitoring/alerts.yaml`)

- `agent_request_duration_seconds`: Thời gian xử lý từng step và toàn bộ graph (cảnh báo `HighLLMLatencyP99` bám đúng tên node `planner`/`dispatch`/`reflector`/`finalize`, đối chiếu mục tiêu SLO/TTFT trong [docs/slos.md](../../docs/slos.md) kết hợp conversational fast-path).
- `agent_tokens_total` & `agent_model_proxy_fallback_total`: Theo dõi token theo model alias và phát hiện fallback ngầm phía LiteLLM server (`TokenMetricsCallbackHandler` đọc `model_name`/`response_metadata` khi proxy định tuyến sang `llama-chat-backup`/`llama-server`/model hạ cấp), kích hoạt cả circuit breaker lẫn cảnh báo `ModelCircuitBreakerTripped`.
- `agent_hitl_approvals_total` & `HITL_PENDING_APPROVALS`: Thống kê tác vụ cần duyệt; `_PENDING_APPROVAL_KEYS` chống tăng đúp khi LangGraph replay node trong lúc resume và chỉ giảm một lần khi hoàn tất/từ chối để alert `HighPendingApprovalsQueue` phản ánh đúng hàng đợi.
- Kube-state-metrics phát hiện vLLM container bị `OOMKilled` và kích hoạt `GPUOOMAlert`; bản rule canonical trong `deploy/monitoring/alerts.yaml` phải đồng nhất với PrometheusRule được triển khai tại `deploy/k8s/monitoring.yaml`.
- Bổ sung nhóm cảnh báo vận hành trọng yếu: `CheckpointPersistErrors`, `RateLimitUnavailable`, và `RAGRetrievalDegraded`.

---

## 3. Khung Đánh Giá Chất Lượng Liên Tục & RAGAS Framework (`evals/`)

Tự động chạy trong CI/CD pipeline (`ci.yml`) và `scripts/generate_prr_evidence.py`:
- **Bộ dữ liệu chuẩn mở rộng**: `evals/datasets/golden_v1.jsonl` (**100** kịch bản routing/risk/tool), `rag_qa.jsonl` (**50** kịch bản RAG gồm cả negative abstention), và `red_teaming.jsonl` (**60** kịch bản); `scripts/seed_eval.py` yêu cầu `--force` mới ghi đè dataset hiện có.
- `evaluators.py` & `answer_generator.py`: Tách model sinh câu trả lời (`eval_generator_model`) khỏi model chấm điểm (`eval_judge_model`); nhận diện chính xác câu từ chối rỗng hợp lệ khi `is_empty_retrieval` đúng; `run_eval.py` gắn rõ nhãn `planner_mode=llm|heuristic_proxy`, fail cứng trong `--mode release` nếu planner bị degraded hoặc dùng proxy, còn `release_gate.py` chỉ chấp nhận hạ tầng LLM/BGE/Qdrant/judge thật.
- `red_teaming_runner.py`: Bộ test **60 kịch bản** (45 tấn công gồm jailbreak paraphrase, ZWSP, base64/URL obfuscation, injection gián tiếp qua RAG, exfiltration tool args, toxicity + 15 câu lành tính bao gồm cụm từ kỹ thuật).
- `generate_prr_evidence.py`: Thực thi thật `ruff`, `mypy --no-incremental`, `pytest tests/ -n auto --cov=src --cov-fail-under=85`, `evals/run_eval.py --mode smoke`, `evals/red_teaming_runner.py`, `scripts/restore_drill.py --smoke`, và kiểm tra cú pháp toàn bộ YAML K8s/CI để sinh [evidence/prr/latest.json](../../evidence/prr/latest.json) (verdict luôn giữ `AUTO-EVIDENCE PENDING REVIEW BOARD` cho đến khi vượt `release_gate.py` + đủ 9 chữ ký).
- `deepeval_runner.py`: Suite đo lường chất lượng agent theo tiêu chuẩn DeepEval.
- `ragas_runner.py`: Đánh giá chuyên sâu hệ thống RAG & Multi-Agent theo **RAGAS framework** gồm 4 chỉ số cốt lõi:
  - **Faithfulness**: Độ trung thực và tính nhất quán factual của câu trả lời so với context.
  - **Answer Relevance**: Độ liên quan trực tiếp của câu trả lời với câu hỏi của người dùng.
  - **Context Precision**: Độ chính xác và thứ tự xếp hạng (ranking order) của các chunk context truy xuất.
  - **Context Recall**: Mức độ bao phủ thông tin từ ground-truth reference trong context.
  - Tự động đồng bộ dataset và log feedback metrics lên **LangSmith** khi bật tracing.
- Không cho phép merge mã nguồn nếu điểm benchmark thấp hơn ngưỡng quy định (`configs/eval_thresholds.yaml`).

---

## 4. Tài Liệu Kiểm Định & Tham Chiếu Liên Quan

- **Kiểm định khả năng quan sát, giám sát & cảnh báo**: [REVIEW_OBSERVABILITY & ALERTING.md](../reviews/REVIEW_OBSERVABILITY%20&%20ALERTING.md)
- **Kiểm định cổng đánh giá chất lượng tác tử (DeepEval & RAGAS)**: [REVIEW_QUALITY GATES - DEEPEVAL & RAGAS.md](../reviews/REVIEW_QUALITY%20GATES%20-%20DEEPEVAL%20&%20RAGAS.md)
- **Kiểm định chiến lược kiểm thử toàn diện & QA**: [REVIEW_TESTING & QA STRATEGY.md](../reviews/REVIEW_TESTING%20&%20QA%20STRATEGY.md)
