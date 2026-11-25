"""Seed script generating evaluation benchmark datasets (safe by default; pass --force to overwrite)."""

import argparse
import json
from pathlib import Path

DATASETS_DIR = Path(__file__).resolve().parents[1] / "evals" / "datasets"
DATASETS_DIR.mkdir(parents=True, exist_ok=True)

GOLDEN_V1: list[dict[str, object]] = [
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
        "expected_risk": "high",
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

RAG_QA: list[dict[str, object]] = [
    {
        "id": "rag_01",
        "question": "Mức độ rủi ro khi chạy lệnh shell bash là gì và có cần duyệt không?",
        "ground_truth": "Mức độ rủi ro cao (risk_level: high) và bắt buộc phải tạm dừng chờ phê duyệt từ người dùng (Human-In-The-Loop - HITL).",
    },
    {
        "id": "rag_02",
        "question": "Sub-agents có được lưu state riêng không?",
        "ground_truth": "Sub-Agents là stateless, không được tự ý lưu trữ trạng thái phiên làm việc cục bộ. Mọi trạng thái và lịch sử phiên phải do LangGraph Supervisor quản lý.",
    },
    {
        "id": "rag_03",
        "question": "Thời gian timeout tối đa cho mỗi tiến trình chạy thử nghiệm code trong sandbox là bao nhiêu giây?",
        "ground_truth": "Giới hạn thời gian timeout tối đa cho mỗi tiến trình chạy thử nghiệm là 30 giây.",
    },
    {
        "id": "rag_04",
        "question": "Quy định về việc bảo vệ dữ liệu PII và API secret token như thế nào?",
        "ground_truth": "Dữ liệu chứa Email, Số điện thoại, API Secret Token phải được phát hiện và ẩn danh hoá (redact) trước khi lưu vào lịch sử hoặc gửi qua LLM bên ngoài.",
    },
    {
        "id": "rag_05",
        "question": "Công cụ Tools phải tuân thủ chuẩn giao thức nào và phân quyền ra sao?",
        "ground_truth": "Các công cụ phải đóng gói theo chuẩn Model Context Protocol (MCP) và chịu sự kiểm soát của Allowlist phân quyền theo từng tác tử.",
    },
    {
        "id": "rag_06",
        "question": "Thao tác ghi đè tệp mã nguồn dự án (repo_write_file) có mức độ rủi ro là gì và có bắt buộc phê duyệt không?",
        "ground_truth": "Thao tác ghi đè tệp mã nguồn dự án được phân loại mức độ rủi ro cao (risk_level: high) và bắt buộc phải tạm dừng chờ phê duyệt từ người dùng (Human-In-The-Loop - HITL).",
    },
    {
        "id": "rag_07",
        "question": "Yêu cầu bảo mật cho các kết nối từ xa (remote connections) theo chính sách là gì?",
        "ground_truth": "Tất cả các kết nối từ xa bắt buộc phải sử dụng mã xác thực hai lớp qua thiết bị phần cứng (Hardware Token 2FA).",
    },
    {
        "id": "rag_08",
        "question": "Chính sách quy định thế nào về các lệnh truy cập tài nguyên mạng từ bên trong sandbox?",
        "ground_truth": "Nghiêm cấm các lệnh truy cập tài nguyên mạng bên ngoài sandbox nếu không có cờ cấp phép tường lửa.",
    },
    {
        "id": "rag_09",
        "question": "Chính sách thưởng cổ phiếu ESOP năm 2030 của công ty quy định tỷ lệ phân bổ như thế nào?",
        "ground_truth": "Hệ thống tài liệu chính sách nội bộ hiện tại không có thông tin về chính sách thưởng cổ phiếu ESOP.",
    },
    {
        "id": "rag_10",
        "question": "Quy trình cấp phát thẻ tín dụng hạn mức công ty cho nhân viên mới quy định tại văn bản nào?",
        "ground_truth": "Hệ thống không tìm thấy thông tin về quy trình cấp phát thẻ tín dụng công ty trong kho tài liệu chính sách nội bộ.",
    },
]


def _write_if_allowed(path: Path, rows: list[dict[str, object]], *, force: bool) -> None:
    if path.exists() and not force:
        existing_lines = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        if existing_lines >= len(rows):
            print(
                f"Skipping {path} (already contains {existing_lines} cases >= seed {len(rows)}; pass --force to overwrite)."
            )
            return
    with open(path, "w", encoding="utf-8") as f:
        for item in rows:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"Wrote {len(rows)} cases to {path}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Seed minimal starter eval datasets.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing larger curated datasets with starter seed fixtures.",
    )
    args = parser.parse_args(argv)
    _write_if_allowed(DATASETS_DIR / "golden_v1.jsonl", GOLDEN_V1, force=args.force)
    _write_if_allowed(DATASETS_DIR / "rag_qa.jsonl", RAG_QA, force=args.force)


if __name__ == "__main__":
    main()
