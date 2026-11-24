# System patterns

Chỉ dùng các pattern dưới đây khi code hiện hữu xác nhận chúng phù hợp.

## LangGraph state và DAG

- Node nhận `AgentState`, trả về dict update; schema ở `src/agentic/orchestrator/state.py`.
- Dispatch chỉ fan-out các step độc lập có dependency đã hoàn tất; DAG invalid/deadlock phải fail rõ ràng.
- Approval gắn với `step_id`; reset run-scoped state ở đầu lượt chat.

## A2A và execution identity

- Tái sử dụng singleton `httpx.AsyncClient` trong `src/agentic/a2a/client.py`.
- Lan truyền owner/run/step và trace metadata; production remote call phải fail closed.
- Capability ngắn hạn được ký và kiểm lại ở cả A2A executor lẫn MCP boundary.

## MCP tool

- Policy owner là `configs/policies.yaml`: risk, approval và per-agent allowlist.
- Schema/tool binding owner là `src/agentic/tools/mcp_client.py`; server vẫn phải tự xác thực S2S/capability.
- Hàm sync được gọi từ async path phải qua `asyncio.to_thread`; không nuốt exception bảo mật.

## Persistence và tenant isolation

- Production checkpoint dùng `AsyncPostgresSaver`; pool PostgreSQL đặt `prepare_threshold=None` khi qua PgBouncer transaction mode.
- Cache, retrieval, checkpoint và long-term memory key/filter phải chứa tenant/owner scope.
- Production không fallback sang local state nếu điều đó làm yếu isolation hoặc durability.

## RAG và cache

- Áp tenant/filter trước lexical/vector fusion và rerank.
- Cache key phải bao gồm mọi input ảnh hưởng kết quả (query chuẩn hóa, model/config, filter/tenant).
- Ingestion/CDC resolve path dưới data root, hỗ trợ upsert/delete và invalidation/rebuild nhất quán.
