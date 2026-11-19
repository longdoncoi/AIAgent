# REVIEW_QUALITY GATES - DEEPEVAL & RAGAS.md — Trụ Cột 6: Cổng Đánh Giá Chất Lượng Tác Tử

> **Tài liệu kiểm định chuyên biệt (Production Readiness Review - Pillar 06 / 14)**  
> **Đơn vị phụ trách**: AI Evaluation Lead & Data Quality Engineer  
> **Tài liệu tham chiếu**: [REVIEW.md](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW.md) · [Architecture.md](file:///f:/PROJECTS/AIAgent/Architecture.md) · [AGENTS.md](file:///f:/PROJECTS/AIAgent/AGENTS.md) · [configs/eval_thresholds.yaml](file:///f:/PROJECTS/AIAgent/configs/eval_thresholds.yaml)

---

## 1. Mục Tiêu & Phạm Vi Kiểm Định

Trụ cột này thiết lập cổng đánh giá chất lượng (Quality Gate) tự động trước khi cho phép merge mã nguồn và phát hành sản phẩm:

1. **Độ chính xác Định tuyến (Routing Accuracy Gate)**: Supervisor điều phối chính xác $\ge 95\%$ tác vụ đến đúng Sub-Agent.
2. **Độ căn cứ & Ảo giác (Groundedness & Hallucination Gate)**: Câu trả lời bám sát context, Groundedness $\ge 0.85$, tỷ lệ ảo giác $\le 5\%$.
3. **Bộ chỉ số RAGAS Framework**:
   - **Faithfulness**: $\ge 0.80$ (Độ trung thực).
   - **Answer Relevance**: $\ge 0.80$ (Độ liên quan câu trả lời).
   - **Context Precision**: $\ge 0.75$ (Độ chính xác xếp hạng chunk).
   - **Context Recall**: $\ge 0.75$ (Độ bao phủ tri thức cần thiết).
4. **Tập Dữ Liệu Kiểm Thử Vàng (Golden Dataset)**: Bảo trì tập `evals/datasets/golden_v1.jsonl` và `rag_qa.jsonl` chứa đủ các trường hợp biên.
5. **Tích hợp LangSmith Run Feedback**: Tự động đồng bộ kết quả chấm điểm lên LangSmith.

---

## 2. Danh Mục Checklist Kiểm Tra Chi Tiết

| ID | Trạng Thái | Mức Độ | Tiêu Chí Kiểm Tra (Verification Criteria) | Tệp Mã Nguồn / Cấu Hình | Phương Pháp & Lệnh Kiểm Chứng |
| :---: | :---: | :---: | :--- | :--- | :--- |
| **6.1** | `[ ]` | **P0** | **Độ Chính Xác Điều Phối (Routing Accuracy Gate)**: Supervisor phải phân rã yêu cầu và điều phối chính xác $\ge 95\%$ tác vụ vào đúng Sub-Agent phụ trách (ChatAgent, RAGAgent, CodingAgent). | [configs/eval_thresholds.yaml](file:///f:/PROJECTS/AIAgent/configs/eval_thresholds.yaml)<br>[evals/evaluators.py](file:///f:/PROJECTS/AIAgent/evals/evaluators.py) | Chạy evaluation gate:<br>`python evals/run_eval.py`<br>*(Ngưỡng yêu cầu: $\ge 0.95$)* |
| **6.2** | `[ ]` | **P0** | **Độ Căn Cứ & Không Ảo Giác (Groundedness & Hallucination Gate)**: Câu trả lời của RAGAgent phải bám sát tài liệu nguồn, Groundedness Score $\ge 0.85$, tỷ lệ ảo giác (Hallucination Ratio) $\le 5\%$. | [configs/eval_thresholds.yaml](file:///f:/PROJECTS/AIAgent/configs/eval_thresholds.yaml)<br>[evals/deepeval_runner.py](file:///f:/PROJECTS/AIAgent/evals/deepeval_runner.py) | Chạy DeepEval Suite:<br>`python evals/deepeval_runner.py`<br>*(Ngưỡng yêu cầu: $\ge 0.85$)* |
| **6.3** | `[ ]` | **P0** | **Bộ Chỉ Số RAGAS Framework Toàn Diện**: Kiểm định 4 chỉ số cốt lõi của RAG: Faithfulness $\ge 0.80$, Answer Relevance $\ge 0.80$, Context Precision $\ge 0.75$, Context Recall $\ge 0.75$. | [configs/eval_thresholds.yaml](file:///f:/PROJECTS/AIAgent/configs/eval_thresholds.yaml)<br>[evals/ragas_runner.py](file:///f:/PROJECTS/AIAgent/evals/ragas_runner.py) | Chạy RAGAS benchmark runner:<br>`python evals/ragas_runner.py` |
| **6.4** | `[ ]` | **P1** | **Tập Dữ Liệu Kiểm Thử Vàng (Golden Benchmark Dataset)**: Tập dữ liệu `evals/datasets/golden_v1.jsonl` và `evals/datasets/rag_qa.jsonl` được đồng bộ, cập nhật đầy đủ các case nghiệp vụ biên (edge cases) và chính sách mới nhất. | [evals/datasets/](file:///f:/PROJECTS/AIAgent/evals/datasets/) | Kiểm tra số lượng mẫu và định dạng JSONL:<br>`wc -l evals/datasets/golden_v1.jsonl` |
| **6.5** | `[ ]` | **P1** | **Đồng Bộ Dữ Liệu Lên LangSmith**: Khi kích hoạt `LANGCHAIN_TRACING_V2=true`, hệ thống phải tự động đồng bộ kết quả đánh giá và log feedback trực tiếp lên LangSmith Project. | [src/agentic/observability/langsmith_tracer.py](file:///f:/PROJECTS/AIAgent/src/agentic/observability/langsmith_tracer.py) | Chạy test tích hợp LangSmith runner:<br>`pytest tests/unit/test_ragas_and_langsmith.py` |

---

## 3. Quy Trình Xác Minh & Lệnh Thực Thi (Pre-Flight Runbook)

### Bước 1: Thực Thi Bộ Ba Runners Đánh Giá Chất Lượng

```bash
# 1. Chạy CI Quality Gate chuẩn
python evals/run_eval.py

# 2. Chạy DeepEval runner
python evals/deepeval_runner.py

# 3. Chạy RAGAS runner
python evals/ragas_runner.py
```

### Bước 2: Kiểm Tra Đối Soát Ngưỡng Trong CI/CD

```bash
pytest tests/unit/test_ragas_and_langsmith.py -v
```

---

## 4. Danh Mục Giám Sát Sau Triển Khai (Post-Deployment Hypercare)

Sau khi hệ thống vận hành chính thức:

- [ ] **T+1 Giờ**: Xác nhận 100% các phiên tương tác được gửi traces và spans đầy đủ lên LangSmith Dashboard (`LANGCHAIN_TRACING_V2=true`).
- [ ] **T+24 Giờ**: Lấy mẫu ngẫu nhiên 50 hội thoại thực tế của người dùng, thực thi LLM-as-a-judge kiểm tra: Groundedness $\ge 0.85$, Faithfulness $\ge 0.80$, tỷ lệ ảo giác $\le 5\%$.
- [ ] **T+7 Ngày**: Thu thập các trường hợp phản hồi bị người dùng đánh giá thấp (negative user feedback / thumbs down) để bổ sung vào Golden Dataset `golden_v1.jsonl`.

---

## 5. Trạng Thái Nghiệm Thu & Liên Kết Ký Duyệt

- **Tổng số tiêu chí kiểm định**: 5 (P0: 3, P1: 2, P2: 0)
- **Đạt**: _____ / 5
- **Kết luận**: [ ] ĐẠT (PASS) &nbsp;&nbsp;&nbsp; [ ] CẦN KHẮC PHỤC (REJECT)

> 🎯 **Biên Bản Ký Duyệt**: Kết quả đánh giá của Trụ cột này được tổng hợp và ký duyệt chính thức tại [Bảng Chữ Ký Phê Chuẩn Của Hội Đồng Đánh Giá (Mục 6.3 - REVIEW.md)](file:///f:/PROJECTS/AIAgent/.workflows/reviews/REVIEW.md#63-bảng-chữ-ký-phê-chuẩn-của-hội-đồng-đánh-giá).
