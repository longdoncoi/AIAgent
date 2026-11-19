# REVIEW_OBSERVABILITY & ALERTING.md — Trụ Cột 8: Khả Năng Quan Sát, Giám Sát & Cảnh Báo

> **Tài liệu kiểm định chuyên biệt (Production Readiness Review - Pillar 08 / 14)**  
> **Đơn vị phụ trách**: SRE Monitoring Lead & Lead Backend Engineer  
> **Tài liệu tham chiếu**: [REVIEW.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW.md) · [Architecture.md](file:///f:/PROJECTS/AIAgent/Architecture.md) · [AGENTS.md](file:///f:/PROJECTS/AIAgent/AGENTS.md) · [deploy/monitoring/](file:///f:/PROJECTS/AIAgent/deploy/monitoring/)

---

## 1. Mục Tiêu & Phạm Vi Kiểm Định

Trụ cột này bảo đảm hệ thống có đầy đủ khả năng quan sát toàn trình (Observability) và hệ thống cảnh báo sớm:

1. **Truy Vết Phân Tán (Distributed Tracing)**: Header chuẩn W3C `traceparent` được truyền xuyên suốt qua các tầng mạng A2A và MCP tools.
2. **Tích Hợp Sâu Tracing & LLM Metadata**: Đồng bộ OpenTelemetry (Jaeger) với LangSmith, ghi nhận đầy đủ prompt, token count và tool calls.
3. **Thu Thập Chỉ Số Định Lượng (Prometheus Metrics)**: Thu thập đa chiều về thời gian xử lý, lưu lượng token, tỷ lệ duyệt HITL, và hiệu suất cache.
4. **Hệ Thống Cảnh Báo Chủ Động (Alertmanager Rules)**: Bộ 5 quy tắc cảnh báo sự cố nghiêm trọng được kiểm thử cú pháp và hoạt động tin cậy.

---

## 2. Danh Mục Checklist Kiểm Tra Chi Tiết

| ID | Trạng Thái | Mức Độ | Tiêu Chí Kiểm Tra (Verification Criteria) | Tệp Mã Nguồn / Cấu Hình | Phương Pháp & Lệnh Kiểm Chứng |
| :---: | :---: | :---: | :--- | :--- | :--- |
| **8.1** | `[ ]` | **P0** | **Truy Vết Phân Tán Xuyên Suốt (Distributed Tracing)**: Toàn bộ A2A Client calls và MCP tool executions phải đính kèm header chuẩn W3C `traceparent`. Trace ID liên kết chính xác giữa Jaeger và LangSmith. | [src/agentic/observability/tracing.py](file:///f:/PROJECTS/AIAgent/src/agentic/observability/tracing.py)<br>[src/agentic/a2a/client.py](file:///f:/PROJECTS/AIAgent/src/agentic/a2a/client.py) | Gửi request chat và kiểm tra trace trên Jaeger UI:<br>`curl http://localhost:16686/api/traces?service=agentic-supervisor` |
| **8.2** | `[ ]` | **P0** | **Đo Lường Chỉ Số Đa Chiều (Prometheus Metrics SLA)**: Cung cấp endpoint `/metrics` thu thập đầy đủ: `agent_request_duration_seconds`, `agent_tokens_total`, `agent_hitl_approvals_total`, `cache_hits_total`. | [src/agentic/observability/metrics.py](file:///f:/PROJECTS/AIAgent/src/agentic/observability/metrics.py) | Kiểm tra metrics export:<br>`curl -s <http://localhost:8000/metrics> | grep agent_` |
| **8.3** | `[ ]` | **P1** | **Cảnh Báo Tự Động Sản Xuất (Alertmanager Rules)**: 5 quy tắc alert trong `deploy/monitoring/alerts.yaml` (`HighLatencyAlert`, `HighErrorRateAlert`, `GPUOOMAlert`, `CircuitBreakerOpenAlert`, `HITLBacklogAlert`) đã kiểm tra cú pháp bằng `promtool`. | [deploy/monitoring/alerts.yaml](file:///f:/PROJECTS/AIAgent/deploy/monitoring/alerts.yaml) | Chạy kiểm tra quy tắc alert:<br>`promtool check rules deploy/monitoring/alerts.yaml` |
| **8.4** | `[ ]` | **P1** | **Tích Hợp Nền Tảng LangSmith Observability**: Khi bật `LANGCHAIN_TRACING_V2=true`, hệ thống tự động ghi nhận spans, tokens, và feedback loops trực tiếp lên LangSmith project. | [src/agentic/observability/langsmith_tracer.py](file:///f:/PROJECTS/AIAgent/src/agentic/observability/langsmith_tracer.py) | Chạy unit test tích hợp LangSmith:<br>`pytest tests/unit/test_ragas_and_langsmith.py` |

---

## 3. Quy Trình Xác Minh & Lệnh Thực Thi (Pre-Flight Runbook)

### Bước 1: Kiểm Tra Cú Pháp Alert Rules Bằng Promtool

```bash
promtool check rules deploy/monitoring/alerts.yaml
```

### Bước 2: Kiểm Tra Endpoint Đo Lường Prometheus

```bash
curl -s http://localhost:8000/metrics | grep "agent_request_duration_seconds_bucket"
```

### Bước 3: Kiểm Tra Tích Hợp Tracing & LangSmith

```bash
pytest tests/unit/test_ragas_and_langsmith.py -v
```

---

## 4. Danh Mục Giám Sát Sau Triển Khai (Post-Deployment Hypercare)

Sau khi hệ thống tiếp nhận lưu lượng truy cập thực tế:

- [ ] **T+15 Phút**: Xác nhận Grafana Dashboards hiển thị đầy đủ metrics từ Prometheus `/metrics`.
- [ ] **T+1 Giờ**: Kiểm tra tỷ lệ lỗi API tổng thể $< 0.05\%$, xác nhận không có cảnh báo Prometheus Alertmanager nào đang kích hoạt (Firing).
- [ ] **T+24 Giờ**: Phân tích biểu đồ phân vị độ trễ P50/P90/P99 và kiểm tra liên kết phân tán W3C `traceparent` qua Jaeger và LangSmith.
- [ ] **T+7 Ngày**: Rà soát các quy tắc cảnh báo để tinh chỉnh ngưỡng trigger, tránh hiện tượng Alert Fatigue cho đội ngũ On-call.

---

## 5. Trạng Thái Nghiệm Thu & Liên Kết Ký Duyệt

- **Tổng số tiêu chí kiểm định**: 4 (P0: 2, P1: 2, P2: 0)
- **Đạt**: _____ / 4
- **Kết luận**: [ ] ĐẠT (PASS) &nbsp;&nbsp;&nbsp; [ ] CẦN KHẮC PHỤC (REJECT)

> 🎯 **Biên Bản Ký Duyệt**: Kết quả đánh giá của Trụ cột này được tổng hợp và ký duyệt chính thức tại [Bảng Chữ Ký Phê Chuẩn Của Hội Đồng Đánh Giá (Mục 6.3 - REVIEW.md)](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW.md#63-bảng-chữ-ký-phê-chuẩn-của-hội-đồng-đánh-giá).
