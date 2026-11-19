# skill-eval-quality-gates.md — Quy Trình Chạy Đánh Giá Chất Lượng AI (DeepEval & RAGAS)

> **Mục tiêu**: Hướng dẫn chạy các bộ kiểm định chất lượng LLM, độ chính xác của Agent và tính tin cậy của hệ thống RAG trước khi phát hành.

---

## 1. Lệnh Thực Thi Bộ Đánh Giá

```bash
# 1. Chạy đánh giá routing & groundedness với LLM-judge
make eval

# 2. Chạy bộ benchmark chuyên sâu DeepEval
make eval-deep

# 3. Chạy bộ đánh giá RAGAS cho pipeline RAG & Multi-Agent
make eval-ragas
```

---

## 2. Tiêu Chuẩn Ngưỡng Đạt (Quality Gate Thresholds)

Dựa trên cấu hình tại `configs/eval_thresholds.yaml`:

| Chỉ số (Metric) | Framework | Ngưỡng Tối Thiểu | Ý nghĩa |
| :--- | :--- | :---: | :--- |
| **Faithfulness** | RAGAS | $\ge 0.85$ | Câu trả lời hoàn toàn dựa trên context truy xuất, không bịa đặt |
| **Answer Relevance** | RAGAS | $\ge 0.85$ | Nội dung trả lời trúng trọng tâm câu hỏi của người dùng |
| **Context Precision** | RAGAS | $\ge 0.80$ | Chunk thông tin quan trọng được xếp ở các thứ hạng đầu |
| **Context Recall** | RAGAS | $\ge 0.80$ | Mức độ bao phủ đầy đủ ý của ground-truth |
| **Routing Accuracy** | DeepEval | $\ge 0.95$ | Supervisor điều phối đúng sub-agent có thẩm quyền |
| **Groundedness** | DeepEval | $\ge 0.90$ | Không có hallucination ngoài phạm vi tri thức nội bộ |

---

## 3. Tích Hợp LangSmith

Khi chạy đánh giá với biến môi trường `LANGCHAIN_TRACING_V2=true`:
- Kết quả và feedback score sẽ tự động đồng bộ lên dashboard của LangSmith.
- Có thể dùng `evals/deepeval_runner.py` để kéo dataset mới từ production về chạy benchmark.
