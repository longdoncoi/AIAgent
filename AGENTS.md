# AGENTS.md — Chỉ dẫn làm việc trong `agentic-platform`

Tệp này là hợp đồng thao tác ngắn gọn cho coding agent. Chỉ nạp tài liệu chi tiết khi nhiệm vụ cần; không đọc toàn bộ `.memory/`, `.workflows/` hoặc `ARCHITECTURE.md` ở đầu mỗi phiên.

## Quy tắc bắt buộc

1. **Bằng chứng trước giả định**: kiểm tra code, test và cấu hình liên quan trước khi sửa. Nếu tài liệu mâu thuẫn với runtime, coi code/config/test là trạng thái hiện hành và sửa tài liệu cùng thay đổi.
2. **Phụ thuộc một chiều**: `L1 API → L2 Supervisor → L3 Sub-Agents → L4 Tools / L5 RAG → L6 Memory / L7 Gateway`. `src/agentic/core/` không phụ thuộc tầng trên.
3. **Async tại biên I/O**: dùng `async`/`await` cho network, DB, cache và model calls. Đưa công việc đồng bộ/CPU-bound có thể chặn event loop qua worker (`asyncio.to_thread` hoặc executor phù hợp).
4. **Kiểu dữ liệu rõ ràng**: code mới phải tương thích `mypy --strict`; dùng Pydantic v2 cho DTO, state và payload ở ranh giới hệ thống.
5. **Fail closed**: không bỏ qua tenant isolation, tool allowlist, capability theo run/step, HITL hay kiểm tra chữ ký. Không ghi secret thật vào repo.
6. **Thay đổi tối thiểu, kiểm chứng tương xứng**: chạy test hẹp trước, sau đó mở rộng theo rủi ro. Không bắt buộc chạy full suite cho thay đổi chỉ có tài liệu; phải nói rõ những kiểm tra chưa chạy.
7. **Giữ nguyên thay đổi của người dùng**: xem `git status`, không hoàn tác hay ghi đè phần ngoài phạm vi.

## Nạp ngữ cảnh theo nhiệm vụ

| Khi cần | Chỉ đọc |
| --- | --- |
| Tiếp tục công việc dở, release hoặc blocker | [`.memory/activeContext.md`](.memory/activeContext.md) |
| Quy ước thao tác chung | [`.context/agent-instructions.md`](.context/agent-instructions.md) |
| Tính năng mới / sửa lỗi / chuẩn bị PR | workflow tương ứng trong [`.workflows/README.md`](.workflows/README.md) |
| Test, lint, migration, eval, thêm agent/tool | skill tương ứng trong [`.skills/README.md`](.skills/README.md) |
| Quyết định hoặc mẫu kiến trúc | tệp liên quan trong [`.memory/README.md`](.memory/README.md) |
| Thay đổi kiến trúc/giao thức | phần liên quan của [`Architecture.md`](Architecture.md) và [`.workflows/specifications/`](.workflows/specifications/) |
| Production Readiness Review | [`.workflows/reviews/REVIEW.md`](.workflows/reviews/REVIEW.md), rồi đúng một trụ cột cần đánh giá |

Tìm theo symbol/path trước (`rg`/`rg --files`), rồi chỉ mở đoạn hoặc tài liệu liên quan. Không dùng README hay memory làm bằng chứng duy nhất cho hành vi runtime.

## Bản đồ hệ thống tối thiểu

- L1 FastAPI/SSE: `src/agentic/api/`
- L2 LangGraph Supervisor: `src/agentic/orchestrator/`
- L3 A2A agents: `src/agentic/agents/`, `src/agentic/a2a/` (`9100–9102`)
- L4 MCP tools: `src/agentic/tools/` (`9001–9003`)
- L5 RAG: `src/agentic/rag/`
- L6 state/memory: `src/agentic/memory/`
- L7 model gateway: `src/agentic/llm/`
- L8 safety: `src/agentic/safety/`
- L9 observability/evals: `src/agentic/observability/`, `evals/`

Nguồn sự thật: dependencies/runtime ở `pyproject.toml` + `uv.lock`; lệnh ở `Makefile`; policy ở `configs/policies.yaml`; port/URL ở `src/agentic/core/config.py` và deployment manifests.

## Hoàn tất nhiệm vụ

- Chạy kiểm tra được định tuyến trong skill/workflow liên quan.
- Cập nhật tài liệu chỉ khi contract, kiến trúc, runbook hoặc trạng thái hiện tại thực sự đổi; không ghi nhật ký phiên vào nhiều tệp.
- Thay đổi kiến trúc/giao thức phải cập nhật `Architecture.md` và specification liên quan. Quyết định bền vững mới cần ADR trong `.memory/architectureDecisions.md`.
- Chỉ cập nhật `.memory/activeContext.md` khi thay đổi ưu tiên, blocker, baseline kiểm chứng hoặc công việc cần bàn giao.
