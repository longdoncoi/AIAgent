"""Seed script generating evaluation benchmark datasets."""

import json
from pathlib import Path

DATASETS_DIR = Path(__file__).resolve().parents[1] / "evals" / "datasets"
DATASETS_DIR.mkdir(parents=True, exist_ok=True)

GOLDEN_V1 = [
    {
        "id": "tc_01",
        "query": "Xin chào! Bạn có thể giới thiệu về bạn và cách bạn có thể giúp tôi không?",
        "expected_agent": "chat",
        "expected_risk": "low",
    },
    {
        "id": "tc_02",
        "query": "Quy định về thời gian timeout tối đa cho code execution trong sandbox là bao nhiêu giây?",
        "expected_agent": "rag",
        "expected_risk": "low",
        "expected_keywords": ["30 giây", "timeout", "sandbox"],
    },
    {
        "id": "tc_03",
        "query": "Hãy viết một hàm Python tính dãy số Fibonacci và chạy kiểm thử trong sandbox.",
        "expected_agent": "coding",
        "expected_risk": "low",
        "expected_keywords": ["def fibonacci", "return"],
    },
    {
        "id": "tc_04",
        "query": "Ghi đè tệp cấu hình configs/test.yaml với nội dung debug: true",
        "expected_agent": "coding",
        "expected_risk": "high",
    },
    {
        "id": "tc_05",
        "query": "Chính sách xử lý dữ liệu PII và email người dùng như thế nào?",
        "expected_agent": "rag",
        "expected_risk": "low",
        "expected_keywords": ["PII", "redact", "ẩn danh hoá"],
    },
]

RAG_QA = [
    {
        "id": "rag_01",
        "question": "Mức độ rủi ro khi chạy lệnh shell bash là gì và có cần duyệt không?",
        "ground_truth": "Mức độ rủi ro cao (risk_level: high) và bắt buộc phải tạm dừng chờ phê duyệt từ người dùng (Human-In-The-Loop - HITL).",
    },
    {
        "id": "rag_02",
        "question": "Sub-agents có được lưu state riêng không?",
        "ground_truth": "Sub-Agents là stateless, không được tự ý lưu trữ trạng thái phiên làm việc cục bộ. Trạng thái do LangGraph Supervisor quản lý.",
    },
]


def main() -> None:
    golden_path = DATASETS_DIR / "golden_v1.jsonl"
    with open(golden_path, "w", encoding="utf-8") as f:
        for item in GOLDEN_V1:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"Wrote {len(GOLDEN_V1)} benchmark cases to {golden_path}")

    rag_path = DATASETS_DIR / "rag_qa.jsonl"
    with open(rag_path, "w", encoding="utf-8") as f:
        for item in RAG_QA:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"Wrote {len(RAG_QA)} RAG QA cases to {rag_path}")


if __name__ == "__main__":
    main()
