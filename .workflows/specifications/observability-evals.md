# AGENTS_OBSERVABILITY_EVALS.md — Quan Sát & Đánh Giá Chất Lượng (Observability & Evals - L9)

> **Tài liệu tham chiếu chuyên sâu** thuộc phân vùng kiến trúc Multi-Agent trong hệ sinh thái `agentic-platform`.  
> Tài liệu gốc: [AGENTS.md](../../AGENTS.md) · [ARCHITECTURE.md](../../Architecture.md).

---

## 1. Truy Vết Phân Tán & LangSmith Observability (`tracing.py`, `langsmith_tracer.py`)

- Sử dụng OpenTelemetry SDK truyền trace qua Jaeger và tích hợp toàn diện **LangSmith** (`LANGCHAIN_TRACING_V2=true`).
- Hỗ trợ decorator `@traceable_operation`, `LangChainTracer` callback, RunTree context manager, quản lý dataset (`sync_evaluation_dataset`) và ghi nhận phản hồi/chấm điểm LLM trực tiếp vào LangSmith (`log_feedback`).
- Mọi A2A RPC request và MCP Tool call đều mang header `traceparent` đồng nhất, liên kết OpenTelemetry trace ID với LangSmith metadata.

---

## 2. Hệ Thống Đo Lường & Chỉ Số Prometheus (`metrics.py`)

- `agent_request_duration_seconds`: Thời gian xử lý từng step và toàn bộ graph.
- `agent_tokens_total`: Số lượng token input/output tiêu thụ phân loại theo model alias.
- `agent_hitl_approvals_total`: Thống kê số lượng tác vụ cần duyệt và tỉ lệ chấp thuận.

---

## 3. Khung Đánh Giá Chất Lượng Liên Tục & RAGAS Framework (`evals/`)

Tự động chạy trong CI/CD pipeline (`ci.yml`):
- `evaluators.py`: LLM-as-a-judge đánh giá độ chính xác định tuyến (routing accuracy) và độ căn cứ (groundedness score >= 0.85).
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

- **Kiểm định khả năng quan sát, giám sát & cảnh báo**: [REVIEW_OBSERVABILITY & ALERTING.md](../../.workflows/reviews/REVIEW_OBSERVABILITY%20&%20ALERTING.md)
- **Kiểm định cổng đánh giá chất lượng tác tử (DeepEval & RAGAS)**: [REVIEW_QUALITY GATES - DEEPEVAL & RAGAS.md](../../.workflows/reviews/REVIEW_QUALITY%20GATES%20-%20DEEPEVAL%20&%20RAGAS.md)
- **Kiểm định chiến lược kiểm thử toàn diện & QA**: [REVIEW_TESTING & QA STRATEGY.md](../../.workflows/reviews/REVIEW_TESTING%20&%20QA%20STRATEGY.md)
