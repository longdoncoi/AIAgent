# Chính Sách Vận Hành, Lưu Trữ & Khả Dụng Cao (Operations & SRE Policy)

## 1. Kiến Trúc Cổng Mô Hình & Dự Phòng (Model Gateway & Failover)
- Tất cả yêu cầu suy luận LLM đi qua LiteLLM Gateway trên cổng `4000` với các alias chuẩn: `reasoning-llm` (GPU vLLM), `fast-llm`, và `coding-llm`.
- Khi cụm GPU chính gặp sự cố, `ModelCircuitBreaker` tự động chuyển lưu lượng sang endpoint dự phòng CPU (`llama-chat-backup`, cổng `8080`) và kích hoạt cảnh báo `ModelCircuitBreakerTripped`.

## 2. Quản Trị Bộ Nhớ, Lưu Trữ & Sao Lưu (State, Redis & Backup Policy)
- Redis được tách làm 2 cụm logic: `REDIS_URL` cho cache (`allkeys-lru`) và `REDIS_STATE_URL` cho trạng thái bảo mật (approval nonce, thread registry, RedisSaver) với chính sách `noeviction`.
- Sao lưu PostgreSQL (`agentic-db-backup`) chạy lúc `02:00 UTC` hằng ngày và sao lưu snapshot Qdrant (`agentic-qdrant-backup`) chạy lúc `02:30 UTC` hằng ngày kèm kiểm tra SHA-256.
- Dữ liệu hội thoại không hoạt động quá 30 ngày và bộ nhớ dài hạn (LTM) quá 90 ngày được dọn tự động bởi CronJob `agentic-checkpoint-retention`.

## 3. Khả Dụng Đa Tenant & Quyền Được Quên (Multi-Tenancy & RTBF)
- Mọi truy vấn RAG ở môi trường production bắt buộc mang `tenant_id` đã xác thực từ JWT, tuyệt đối không suy diễn `tenant_id` từ `owner_id`.
- Quy trình Right-to-be-Forgotten (RTBF) mặc định chỉ xoá dữ liệu thuộc sở hữu của `user_id` (`owner_id`), không xoá kho tri thức dùng chung của tổ chức trừ khi có yêu cầu `purge_tenant_corpus=true` kèm `tenant_id` tường minh.
