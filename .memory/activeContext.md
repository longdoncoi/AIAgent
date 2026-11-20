# activeContext.md — Trạng Thái Ngữ Cảnh Hoạt Động Hiện Tại

> Tệp này ghi nhận trạng thái công việc hiện tại, những việc vừa hoàn thành, việc đang làm dở và các bước tiếp theo.

---

## 1. Trạng Thái Hiện Tại (Current Focus)

- **Mục tiêu chính**: Thiết lập hệ thống Workflow, Skills, Memory và Specifications chuẩn hóa cho toàn bộ dự án `agentic-platform`.
- **Sprint / Milestone**: Chuẩn hóa nền tảng phát triển (Development Infrastructure & AI Agent Harness v1.0).

---

## 2. Các Công Việc Vừa Hoàn Thành (Recent Accomplishments)

- [x] Thiết lập cấu trúc thư mục mới: `.skills/`, `.memory/`, `.workflows/`.
- [x] Di chuyển và phân loại toàn bộ 24 tài liệu đặc tả và PRR review từ `AGENTS/` sang `.workflows/specifications/` và `.workflows/reviews/`.
- [x] Cập nhật toàn bộ internal links trong các tài liệu đặc tả & review sang đường dẫn mới.
- [x] Xây dựng bộ quy trình chuẩn trong `.workflows/` (`coding-workflow.md`, `feature-lifecycle.md`, `bugfix-workflow.md`, `pr-preflight.md`, `README.md`).
- [x] Xây dựng kho 6 kỹ năng thực thi trong `.skills/` (`skill-testing-coverage.md`, `skill-add-subagent.md`, `skill-add-mcp-tool.md`, `skill-db-migration.md`, `skill-eval-quality-gates.md`, `skill-code-quality.md`, `README.md`).
- [x] Khởi tạo hệ thống bộ nhớ `.memory/` (`activeContext.md`, `systemPatterns.md`, `techStack.md`, `architectureDecisions.md`, `troubleshooting.md`, `README.md`).
- [x] Chuẩn hóa chỉ dẫn tác chiến cho AI Coding Agent vào [.context/agent-instructions.md](file:///f:/PROJECTS/AIAgent/.context/agent-instructions.md) (chuyển từ `AGENT.md` để làm sạch thư mục gốc).
- [x] Chuyển `REVIEW.md` vào `.workflows/reviews/REVIEW.md` làm Master Dashboard và cập nhật toàn bộ liên kết nội bộ & bên ngoài.
- [x] Cập nhật đồng bộ các liên kết trong `AGENTS.md`, `ARCHITECTURE.md`, `Makefile` và `.workflows/README.md`.
- [x] Xóa an toàn và dọn dẹp thư mục cũ `AGENTS/`.
- [x] Tinh chỉnh chuẩn hóa 10/10: Cập nhật `.gitignore` (chặn `.ruff_cache/`, `.mypy_cache/`, `.deepeval/`) và tạo tệp chỉ mục [.context/README.md](file:///f:/PROJECTS/AIAgent/.context/README.md).
- [x] **Triển khai toàn diện 5 khuyến nghị nâng cấp Production Hardening**:
  1. *Dynamic Hot-Reload*: Cho phép nạp nóng chính sách `policy.yaml`, reset circuit breaker của LLM Gateway và xóa transient cache thông qua API Admin `/api/v1/admin/reload-configs` mà không cần khởi động lại container.
  2. *Prometheus Sub-Second Buckets & TTFT*: Tinh chỉnh histogram `REQUEST_DURATION` xuống đến `0.01s` và bổ sung metric `TTFT_DURATION` (Time-to-First-Token) đo lường chính xác độ trễ streaming token đầu tiên trong `chat_stream_endpoint`.
  3. *Flexible Checkpoint Eviction & Per-Thread TTL*: Hỗ trợ TTL tùy biến theo từng lu hội thoại (`checkpoint_ttl`) trong `RedisSaver`, kèm phương thức dọn dẹp các checkpoint luồng không hoạt động `cleanup_inactive_threads()` và API `/api/v1/admin/cleanup-checkpoints`.
  4. *Selective Semantic Cache Invalidation*: Cập nhật `RAGCache` với cơ chế huỷ cache chọn lọc theo nguồn và tài liệu (`invalidate_by_source`, `invalidate_document`) trên cả L1 memory và L2 Redis, tích hợp vào quy trình CDC delta ingestion.
  5. *Mutual TLS (mTLS) cho A2A Communications*: Bổ sung cấu hình chứng chỉ client mTLS trong `Settings` và tích hợp `A2AClient` hỗ trợ mã hóa 2 chiều cấp Pod.
- [x] Bổ sung bộ kiểm thử `tests/unit/test_next_gen_recommendations.py` (10/10 passed), đạt 257/257 bài test pass toàn dự án với độ bao phủ kiểm thử duy trì ở mức đỉnh cao **99.5%**, 0 lỗi Ruff, 0 lỗi Mypy.

---

## 3. Các Bước Tiếp Theo (Immediate Next Steps)

1. Tiếp tục phát triển các tính năng nghiệp vụ theo quy trình trong `.workflows/feature-lifecycle.md`.
2. Kiểm tra chất lượng và chạy pre-flight khi có thay đổi mã nguồn (`make pre-flight`).
3. Duy trì đồng bộ trạng thái công việc tại `.memory/activeContext.md`.

---

## 4. Các Vấn Đề Đang Tồn Đọng / Blockers (Current Blockers)

- *Không có blocker kỹ thuật nào vào thời điểm hiện tại.*
