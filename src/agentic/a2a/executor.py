"""A2A AgentExecutor: Exposing LangGraph graphs over standardized A2A protocol."""

import asyncio
import hashlib
import hmac
import json
import logging
import socket
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from langchain_core.messages import HumanMessage, ToolMessage
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from src.agentic.a2a.cards import AgentCard, add_agent_card_discovery_route
from src.agentic.a2a.identity import verify_request
from src.agentic.core.config import get_settings
from src.agentic.core.errors import ToolCallApprovalRequired
from src.agentic.core.execution_context import (
    ToolExecutionContext,
    reset_tool_execution_context,
    set_tool_execution_context,
)
from src.agentic.rag.tools import preserve_rag_citations
from src.agentic.safety.guardrails import get_guardrails
from src.agentic.tools.mcp_client import get_mcp_client_manager

logger = logging.getLogger(__name__)


class A2ATaskPayload(BaseModel):
    instruction: str = Field(..., description="Actionable prompt instruction for the sub-agent")
    context: dict[str, Any] = Field(
        default_factory=dict, description="Contextual inputs (e.g. repo path, prior outputs)"
    )


class A2ATaskRequest(BaseModel):
    task_id: str = Field(..., description="Unique step or task identifier")
    caller: str = Field(default="supervisor", description="Calling agent or supervisor")
    target_agent: str = Field(..., description="Name of the targeted agent")
    payload: A2ATaskPayload
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(
        default=None,
        description="Optional idempotency key to deduplicate retried step executions",
    )


class A2ATaskResponse(BaseModel):
    task_id: str
    status: Literal["completed", "failed"]
    output: str
    error: str | None = None


class A2AAgentExecutor:
    """Wraps a compiled LangGraph agent graph into an A2A HTTP service."""

    def __init__(self, card: AgentCard, graph: Any) -> None:
        self.card = card
        self.graph = graph
        self._idempotency_cache: dict[str, tuple[float, A2ATaskResponse]] = {}
        self._idempotency_locks: dict[str, asyncio.Lock] = {}

    def _get_idempotency_lock(self, key: str) -> asyncio.Lock:
        lock = self._idempotency_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._idempotency_locks[key] = lock
        return lock

    def _evict_expired_idempotency(self, now: float) -> None:
        expired = [k for k, (exp, _) in self._idempotency_cache.items() if exp <= now]
        for k in expired:
            self._idempotency_cache.pop(k, None)
            self._idempotency_locks.pop(k, None)
        while len(self._idempotency_cache) > 1024:
            oldest = next(iter(self._idempotency_cache))
            self._idempotency_cache.pop(oldest, None)

    def _execution_context(self, request: A2ATaskRequest) -> ToolExecutionContext:
        """Derive authorization from a signed grant, never from caller booleans."""
        metadata = request.metadata
        if "hitl_approved" in metadata:
            logger.warning("Ignoring caller-supplied 'hitl_approved' flag in A2A metadata")
        return ToolExecutionContext(
            agent_name=self.card.name,
            step_id=request.task_id,
            approval_token=str(metadata["approval_token"]) if metadata.get("approval_token") else None,
            owner_id=str(metadata["owner_id"]) if metadata.get("owner_id") else None,
            step_fingerprint=str(metadata["step_fingerprint"])
            if metadata.get("step_fingerprint")
            else None,
            user_roles=tuple(str(role).lower() for role in (metadata.get("user_roles") or [])),
            department=str(metadata["department"]).lower()
            if metadata.get("department")
            else None,
            client_id=str(metadata["client_id"]) if metadata.get("client_id") else None,
            tenant_id=str(metadata["tenant_id"]) if metadata.get("tenant_id") else None,
        )

    def _safe_message(self, request: A2ATaskRequest) -> HumanMessage:
        """Sanitize every untrusted field before it enters the sub-agent prompt."""
        guardrails = get_guardrails()
        parts = [guardrails.sanitize_input(request.payload.instruction)]
        if request.payload.context:
            safe_context = guardrails.sanitize_context(request.payload.context)
            if safe_context:
                parts.append(
                    "\nAdditional Context: "
                    + json.dumps(safe_context, ensure_ascii=False, default=str)
                )
        return HumanMessage(content="".join(parts))

    def _shared_idempotency_client(self) -> Any | None:
        settings = get_settings()
        if settings.environment == "test":
            return None
        target_url = settings.redis_state_url or settings.redis_url
        if not target_url:
            return None
        try:
            from src.agentic.core.redis import get_redis_client

            return get_redis_client(target_url)
        except Exception:
            return None

    async def _get_cached_idempotent_response(self, cache_key: str) -> A2ATaskResponse | None:
        import time

        now = time.monotonic()
        self._evict_expired_idempotency(now)
        cached = self._idempotency_cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]
        client = self._shared_idempotency_client()
        if client is not None:
            try:
                raw = await asyncio.to_thread(client.get, f"agentic:a2a:idem:{cache_key}")
                if raw:
                    data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
                    resp = A2ATaskResponse(**data)
                    self._idempotency_cache[cache_key] = (now + 300.0, resp)
                    return resp
            except Exception as exc:
                logger.debug("Shared idempotency read skipped: %s", exc)
        return None

    async def _store_idempotent_response(self, cache_key: str, response: A2ATaskResponse) -> None:
        import time

        if response.status != "completed":
            return
        now = time.monotonic()
        self._idempotency_cache[cache_key] = (now + 300.0, response)
        client = self._shared_idempotency_client()
        if client is not None:
            try:
                await asyncio.to_thread(
                    client.set,
                    f"agentic:a2a:idem:{cache_key}",
                    response.model_dump_json(),
                    ex=300,
                )
            except Exception as exc:
                logger.debug("Shared idempotency write skipped: %s", exc)

    def _scoped_idempotency_key(self, request: A2ATaskRequest) -> str | None:
        """Scopes request idempotency keys server-side by tenant, owner, thread, and turn."""
        idem_key = request.idempotency_key
        if not idem_key:
            return None
        meta = request.metadata or {}
        tenant_id = str(meta.get("tenant_id") or "")
        owner_id = str(meta.get("owner_id") or "")
        thread_id = str(meta.get("thread_id") or "")
        turn_id = str(meta.get("turn_id") or "")
        raw = f"{self.card.name}:{tenant_id}:{owner_id}:{thread_id}:{turn_id}:{idem_key}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def execute(self, request: A2ATaskRequest) -> A2ATaskResponse:
        """Executes a single step via the agent's internal LangGraph graph (idempotent when keyed)."""
        verify_request(request, get_settings())
        cache_key = self._scoped_idempotency_key(request)
        if cache_key is not None:
            cached_resp = await self._get_cached_idempotent_response(cache_key)
            if cached_resp is not None:
                return cached_resp
            lock = self._get_idempotency_lock(cache_key)
            try:
                async with lock:
                    cached_resp = await self._get_cached_idempotent_response(cache_key)
                    if cached_resp is not None:
                        return cached_resp
                    response = await self._execute_uncached(request)
                    await self._store_idempotent_response(cache_key, response)
                    return response
            finally:
                # Cancellation or an unexpected storage failure must not retain
                # one lock per attacker-controlled idempotency key forever.
                if cache_key not in self._idempotency_cache:
                    self._idempotency_locks.pop(cache_key, None)
        return await self._execute_uncached(request)

    async def _execute_uncached(self, request: A2ATaskRequest) -> A2ATaskResponse:
        """Executes a single step via the agent's internal LangGraph graph."""
        context_token = set_tool_execution_context(self._execution_context(request))
        try:
            approved_call = request.metadata.get("approved_tool_call")
            if isinstance(approved_call, dict) and approved_call.get("tool_name"):
                tool_name = str(approved_call["tool_name"])
                arguments = dict(approved_call.get("arguments") or {})
                output = await get_mcp_client_manager()._invoke(
                    self.card.name, tool_name, **arguments
                )
                return A2ATaskResponse(
                    task_id=request.task_id,
                    status="completed",
                    output=output,
                )
            initial_state = {"messages": [self._safe_message(request)]}

            result_state = await self.graph.ainvoke(initial_state)

            # Extract the last AI response and preserve structured RAG citations
            messages = result_state.get("messages", [])
            tool_outputs = [
                str(msg.content)
                for msg in messages
                if isinstance(msg, ToolMessage) and getattr(msg, "content", None)
            ]
            output = ""
            for msg in reversed(messages):
                if (
                    hasattr(msg, "content")
                    and msg.content
                    and not isinstance(msg, (HumanMessage, ToolMessage))
                ):
                    output = str(msg.content)
                    break

            if not output and messages:
                output = str(messages[-1].content)

            if tool_outputs:
                output = preserve_rag_citations(output, tool_outputs)

            return A2ATaskResponse(
                task_id=request.task_id,
                status="completed",
                output=output or "Task finished with empty response.",
            )
        except ToolCallApprovalRequired as req_exc:
            return A2ATaskResponse(
                task_id=request.task_id,
                status="failed",
                output="",
                error=json.dumps(
                    {
                        "type": "tool_call_approval_required",
                        "tool_name": req_exc.tool_name,
                        "arguments": req_exc.arguments,
                        "args_digest": req_exc.args_digest,
                    },
                    default=str,
                ),
            )
        except Exception as e:
            error_id = uuid.uuid4().hex[:12]
            logger.error(
                "Error executing A2A task in %s [error_id=%s]: %s",
                self.card.name,
                error_id,
                e,
                exc_info=True,
            )
            return A2ATaskResponse(
                task_id=request.task_id,
                status="failed",
                output="",
                error=f"execution_failed (error_id={error_id})",
            )
        finally:
            reset_tool_execution_context(context_token)

    async def execute_stream(self, request: A2ATaskRequest) -> AsyncIterator[dict[str, Any]]:
        """Streams tokens and execution lifecycle events from the sub-agent graph."""
        verify_request(request, get_settings())
        if isinstance(request.metadata.get("approved_tool_call"), dict):
            response = await self.execute(request)
            yield {"event": "start", "task_id": request.task_id, "agent": self.card.name}
            yield {
                "event": "completed" if response.status == "completed" else "error",
                "task_id": request.task_id,
                "output": response.output,
                "error": response.error,
            }
            return
        cache_key = self._scoped_idempotency_key(request)
        if cache_key is not None:
            cached_resp = await self._get_cached_idempotent_response(cache_key)
            if cached_resp is not None:
                yield {"event": "start", "task_id": request.task_id, "agent": self.card.name}
                yield {
                    "event": "token",
                    "task_id": request.task_id,
                    "agent": self.card.name,
                    "token": cached_resp.output,
                }
                yield {
                    "event": "completed",
                    "task_id": request.task_id,
                    "agent": self.card.name,
                    "output": cached_resp.output,
                }
                return

        accumulated_output: list[str] = []
        tool_outputs: list[str] = []
        context_token = set_tool_execution_context(self._execution_context(request))
        try:
            initial_state = {"messages": [self._safe_message(request)]}
            yield {
                "event": "start",
                "task_id": request.task_id,
                "agent": self.card.name,
            }
            if hasattr(self.graph, "astream_events"):
                async for event in self.graph.astream_events(initial_state, version="v2"):
                    kind = event.get("event")
                    if kind == "on_chat_model_stream":
                        chunk = event.get("data", {}).get("chunk")
                        if chunk and hasattr(chunk, "content") and chunk.content:
                            token = str(chunk.content)
                            accumulated_output.append(token)
                            yield {
                                "event": "token",
                                "task_id": request.task_id,
                                "agent": self.card.name,
                                "token": token,
                            }
                    elif kind == "on_tool_end":
                        tool_result = event.get("data", {}).get("output")
                        if tool_result is not None:
                            text = (
                                str(tool_result.content)
                                if hasattr(tool_result, "content")
                                else str(tool_result)
                            )
                            if text:
                                tool_outputs.append(text)
            else:
                res = await self.execute(request)
                if res.status == "failed":
                    try:
                        err_payload = json.loads(res.error or "{}")
                    except Exception:
                        err_payload = {}
                    if (
                        isinstance(err_payload, dict)
                        and err_payload.get("type") == "tool_call_approval_required"
                    ):
                        yield {
                            "event": "approval_required",
                            "task_id": request.task_id,
                            "agent": self.card.name,
                            "tool_name": str(err_payload.get("tool_name") or ""),
                            "arguments": dict(err_payload.get("arguments") or {}),
                            "args_digest": str(err_payload.get("args_digest") or ""),
                        }
                        return
                    yield {
                        "event": "error",
                        "task_id": request.task_id,
                        "agent": self.card.name,
                        "error": res.error or "execution_failed",
                    }
                    return
                yield {
                    "event": "token",
                    "task_id": request.task_id,
                    "agent": self.card.name,
                    "token": res.output,
                }
                accumulated_output.append(res.output)

            joined_output = "".join(accumulated_output)
            final_output = (
                preserve_rag_citations(joined_output, tool_outputs)
                if tool_outputs
                else joined_output
            )
            if len(final_output) > len(joined_output) and final_output.startswith(joined_output):
                yield {
                    "event": "token",
                    "task_id": request.task_id,
                    "agent": self.card.name,
                    "token": final_output[len(joined_output) :],
                }

            if cache_key is not None:
                await self._store_idempotent_response(
                    cache_key,
                    A2ATaskResponse(
                        task_id=request.task_id,
                        status="completed",
                        output=final_output,
                    ),
                )

            yield {
                "event": "completed",
                "task_id": request.task_id,
                "agent": self.card.name,
                "output": final_output,
            }
        except ToolCallApprovalRequired as req_exc:
            yield {
                "event": "approval_required",
                "task_id": request.task_id,
                "agent": self.card.name,
                "tool_name": req_exc.tool_name,
                "arguments": req_exc.arguments,
                "args_digest": req_exc.args_digest,
            }
            return
        except Exception as e:
            error_id = uuid.uuid4().hex[:12]
            logger.error(
                "Error in streaming A2A task in %s [error_id=%s]: %s",
                self.card.name,
                error_id,
                e,
                exc_info=True,
            )
            yield {
                "event": "error",
                "task_id": request.task_id,
                "agent": self.card.name,
                "error": f"execution_failed (error_id={error_id})",
            }
        finally:
            reset_tool_execution_context(context_token)


def _tcp_reachable(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def create_a2a_server(
    card: AgentCard,
    graph: Any,
    readiness_check: Callable[[], Awaitable[tuple[bool, dict[str, str]]]] | None = None,
    lifespan: Any | None = None,
) -> FastAPI:
    """Creates a FastAPI app implementing the A2A protocol endpoints."""
    app = FastAPI(title=f"A2A - {card.name}", version=card.version, lifespan=lifespan)
    executor = A2AAgentExecutor(card=card, graph=graph)
    settings = get_settings()
    # A2A-standard discovery endpoint (card at /.well-known/agent.json).
    add_agent_card_discovery_route(app, card)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "agent": card.name}

    @app.get("/ready")
    async def ready() -> Response:
        """Readiness probe verifying downstream agent dependencies."""
        checks: dict[str, str] = {}
        is_ready = True
        if settings.require_model_gateway_ready:
            parsed = urlparse(settings.litellm_base_url)
            gw_ok = await asyncio.to_thread(
                _tcp_reachable,
                parsed.hostname or "localhost",
                parsed.port or (443 if parsed.scheme == "https" else 80),
            )
            checks["model_gateway"] = "ok" if gw_ok else "offline"
            if not gw_ok:
                is_ready = False
        if readiness_check is not None:
            dep_ok, dep_checks = await readiness_check()
            checks.update(dep_checks)
            if not dep_ok:
                is_ready = False
        return JSONResponse(
            status_code=status.HTTP_200_OK if is_ready else status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "ready" if is_ready else "unhealthy",
                "agent": card.name,
                "checks": checks,
            },
        )

    @app.get("/metrics")
    async def metrics() -> Response:
        """Expose the process registry scraped by the agent ServiceMonitor."""
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/a2a/card")
    async def get_card() -> AgentCard:
        return card

    def _valid_s2s(service_token: str | None) -> bool:
        if not service_token:
            return False
        valid_tokens = [settings.s2s_internal_token, *settings.s2s_accepted_tokens]
        return any(tok and hmac.compare_digest(service_token, tok) for tok in valid_tokens)

    @app.post("/a2a/tasks", response_model=A2ATaskResponse)
    async def run_task(
        request: A2ATaskRequest,
        raw_request: Request,
        service_token: str | None = Header(default=None, alias="X-Internal-Service-Token"),
    ) -> A2ATaskResponse:
        # S2S Authentication check
        if settings.require_authentication and not _valid_s2s(service_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: Invalid internal service-to-service authentication token.",
            )

        if request.target_agent != card.name and request.target_agent != card.name.replace(
            "_agent", ""
        ):
            raise HTTPException(
                status_code=400,
                detail=f"Target agent mismatch: expected '{card.name}', got '{request.target_agent}'",
            )

        # Distributed Tracing context extraction
        tracer = trace.get_tracer("agentic-a2a")
        extracted_context = TraceContextTextMapPropagator().extract(carrier=raw_request.headers)
        with tracer.start_as_current_span(
            f"a2a.agent.{card.name}", context=extracted_context
        ) as span:
            span.set_attribute("a2a.caller", request.caller)
            span.set_attribute("a2a.task_id", request.task_id)
            return await executor.execute(request)

    @app.post("/a2a/tasks/stream")
    async def run_task_stream(
        request: A2ATaskRequest,
        raw_request: Request,
        service_token: str | None = Header(default=None, alias="X-Internal-Service-Token"),
    ) -> EventSourceResponse:
        """SSE streaming endpoint for fine-grained token streaming from sub-agents."""
        if settings.require_authentication and not _valid_s2s(service_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: Invalid internal service-to-service authentication token.",
            )

        if request.target_agent != card.name and request.target_agent != card.name.replace(
            "_agent", ""
        ):
            raise HTTPException(
                status_code=400,
                detail=f"Target agent mismatch: expected '{card.name}', got '{request.target_agent}'",
            )

        tracer = trace.get_tracer("agentic-a2a")
        extracted_context = TraceContextTextMapPropagator().extract(carrier=raw_request.headers)

        async def _stream_generator() -> AsyncIterator[dict[str, str]]:
            with tracer.start_as_current_span(
                f"a2a.agent.{card.name}.stream", context=extracted_context
            ) as span:
                span.set_attribute("a2a.caller", request.caller)
                span.set_attribute("a2a.task_id", request.task_id)
                span.set_attribute("a2a.stream", True)
                async for ev in executor.execute_stream(request):
                    yield {
                        "event": str(ev.get("event", "message")),
                        "data": json.dumps(ev, ensure_ascii=False),
                    }

        return EventSourceResponse(_stream_generator())

    return app
