"""LangGraph adapters for the official A2A v1 JSON-RPC protocol."""

from __future__ import annotations

import hmac
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from a2a.auth.user import User
from a2a.helpers import new_task_from_user_message, new_text_message, new_text_part
from a2a.server.agent_execution import AgentExecutor as ProtocolAgentExecutor
from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.routes.common import ServerCallContextBuilder
from a2a.server.tasks import DatabaseTaskStore, InMemoryTaskStore, TaskUpdater
from fastapi import FastAPI, Header, HTTPException, Request, status
from langchain_core.messages import HumanMessage
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sse_starlette.sse import EventSourceResponse

from src.agentic.a2a.cards import AgentCard
from src.agentic.core.config import Settings, get_settings
from src.agentic.safety.capabilities import verify_tool_capability
from src.agentic.tools.execution_context import (
    ToolExecutionContext,
    reset_tool_execution_context,
    set_tool_execution_context,
)

logger = logging.getLogger(__name__)


class _AuthenticatedUser(User):
    def __init__(self, owner_id: str) -> None:
        self._owner_id = owner_id

    @property
    def is_authenticated(self) -> bool:
        return bool(self._owner_id)

    @property
    def user_name(self) -> str:
        return self._owner_id


class _A2ACallContextBuilder(ServerCallContextBuilder):
    """Bind persistent A2A tasks to the authenticated owner header."""

    def build(self, request: Request) -> ServerCallContext:
        owner_id = request.headers.get("X-Owner-ID", "")
        return ServerCallContext(
            user=_AuthenticatedUser(owner_id),
            tenant=owner_id,
            state={"headers": dict(request.headers)},
        )


class A2ATaskPayload(BaseModel):
    """Compatibility DTO used by the in-process development adapter."""

    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(..., min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)


class A2ATaskRequest(BaseModel):
    """Compatibility DTO; remote traffic uses the official protobuf schema."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(..., min_length=1)
    caller: str = Field(default="supervisor", min_length=1)
    target_agent: str = Field(..., min_length=1)
    payload: A2ATaskPayload
    metadata: dict[str, Any] = Field(default_factory=dict)


class A2ATaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: Literal["completed", "failed"]
    output: str
    error: str | None = None


class A2AAgentExecutor:
    """Execute one LangGraph step with a verified, request-scoped capability."""

    def __init__(self, card: AgentCard, graph: Any) -> None:
        self.card = card
        self.graph = graph

    def _execution_context(self, request: A2ATaskRequest) -> ToolExecutionContext:
        owner_id = str(request.metadata.get("owner_id") or "")
        run_id = str(request.metadata.get("run_id") or "")
        capability = request.metadata.get("approval_capability")
        capability_token = str(capability) if capability else None
        claims = verify_tool_capability(
            capability_token,
            run_id=run_id,
            step_id=request.task_id,
            owner_id=owner_id,
            agent_name=self.card.name,
        )
        if get_settings().require_authentication and claims is None:
            raise PermissionError("A valid step-scoped capability is required.")
        return ToolExecutionContext(
            agent_name=self.card.name,
            step_id=request.task_id,
            hitl_approved=bool(claims and claims.allowed_tools),
            owner_id=claims.owner_id if claims else None,
            run_id=claims.run_id if claims else None,
            approval_capability=capability_token if claims else None,
            allowed_tools=claims.allowed_tools if claims else (),
        )

    async def execute(self, request: A2ATaskRequest) -> A2ATaskResponse:
        """Execute a single step. Errors are sanitized in production."""
        try:
            execution_context = self._execution_context(request)
        except PermissionError as exc:
            return A2ATaskResponse(
                task_id=request.task_id, status="failed", output="", error=str(exc)
            )

        context_token = set_tool_execution_context(execution_context)
        try:
            context_str = ""
            if request.payload.context:
                context_str = f"\nAdditional Context: {request.payload.context}"
            initial_state = {
                "messages": [HumanMessage(content=request.payload.instruction + context_str)]
            }
            result_state = await self.graph.ainvoke(initial_state)
            messages = result_state.get("messages", [])
            output = ""
            for message in reversed(messages):
                if (
                    hasattr(message, "content")
                    and message.content
                    and not isinstance(message, HumanMessage)
                ):
                    output = str(message.content)
                    break
            if not output and messages:
                output = str(messages[-1].content)
            return A2ATaskResponse(
                task_id=request.task_id,
                status="completed",
                output=output or "Task finished with empty response.",
            )
        except Exception as exc:
            logger.exception("A2A graph execution failed for %s", self.card.name)
            error = "Agent execution failed."
            if get_settings().environment != "production":
                error = str(exc)
            return A2ATaskResponse(task_id=request.task_id, status="failed", output="", error=error)
        finally:
            reset_tool_execution_context(context_token)

    async def execute_stream(self, request: A2ATaskRequest) -> AsyncIterator[dict[str, Any]]:
        """Stream LangGraph tokens for the development compatibility path."""
        execution_context = self._execution_context(request)
        context_str = ""
        if request.payload.context:
            context_str = f"\nAdditional Context: {request.payload.context}"
        initial_state = {
            "messages": [HumanMessage(content=request.payload.instruction + context_str)]
        }
        accumulated_output: list[str] = []
        context_token = set_tool_execution_context(execution_context)
        try:
            yield {"event": "start", "task_id": request.task_id, "agent": self.card.name}
            if hasattr(self.graph, "astream_events"):
                async for event in self.graph.astream_events(initial_state, version="v2"):
                    if event.get("event") == "on_chat_model_stream":
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
            else:
                result_state = await self.graph.ainvoke(initial_state)
                messages = result_state.get("messages", [])
                output = str(messages[-1].content) if messages else ""
                accumulated_output.append(output)
                yield {
                    "event": "token",
                    "task_id": request.task_id,
                    "agent": self.card.name,
                    "token": output,
                }
            yield {
                "event": "completed",
                "task_id": request.task_id,
                "agent": self.card.name,
                "output": "".join(accumulated_output),
            }
        except Exception as exc:
            logger.exception("A2A streaming execution failed for %s", self.card.name)
            error = (
                str(exc)
                if get_settings().environment != "production"
                else "Agent execution failed."
            )
            yield {
                "event": "error",
                "task_id": request.task_id,
                "agent": self.card.name,
                "error": error,
            }
        finally:
            reset_tool_execution_context(context_token)


class A2AProtocolExecutor(ProtocolAgentExecutor):
    """Official A2A executor that delegates work to the internal LangGraph adapter."""

    def __init__(self, executor: A2AAgentExecutor) -> None:
        self._executor = executor

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context.task_id or not context.context_id:
            raise ValueError("A2A request is missing task or context identity.")
        if context.current_task is None:
            if context.message is None:
                raise ValueError("A2A request is missing the user message.")
            await event_queue.enqueue_event(new_task_from_user_message(context.message))
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work()
        metadata = context.metadata
        extra_context = metadata.get("context")
        step_id = str(metadata.get("step_id") or "")
        if not step_id:
            raise ValueError("A2A request metadata is missing the execution step ID.")
        request = A2ATaskRequest(
            task_id=step_id,
            target_agent=self._executor.card.name,
            payload=A2ATaskPayload(
                instruction=context.get_user_input(),
                context=extra_context if isinstance(extra_context, dict) else {},
            ),
            metadata=metadata,
        )
        response = await self._executor.execute(request)
        if response.status == "failed":
            await updater.failed(new_text_message(response.error or "Agent execution failed."))
            return
        await updater.add_artifact([new_text_part(response.output)], last_chunk=True)
        await updater.complete(new_text_message(response.output))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context.task_id or not context.context_id:
            raise ValueError("A2A cancellation is missing task or context identity.")
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


def _postgres_async_url(uri: str) -> str:
    if uri.startswith("postgresql+psycopg://"):
        return uri
    if uri.startswith("postgresql://"):
        return uri.replace("postgresql://", "postgresql+psycopg://", 1)
    raise ValueError("A2A persistent task storage requires a PostgreSQL URI.")


def _task_store(
    settings: Settings,
) -> tuple[DatabaseTaskStore | InMemoryTaskStore, AsyncEngine | None]:
    if settings.environment != "production":
        return InMemoryTaskStore(), None
    engine = create_async_engine(
        _postgres_async_url(settings.postgres_uri),
        pool_pre_ping=True,
        pool_recycle=300,
    )
    return DatabaseTaskStore(engine, create_table=False, table_name="a2a_tasks"), engine


def _check_service_auth(request: Request, card: AgentCard, settings: Settings) -> None:
    token = request.headers.get("X-Internal-Service-Token")
    if not token or not hmac.compare_digest(token, settings.s2s_internal_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    owner_id = request.headers.get("X-Owner-ID", "")
    run_id = request.headers.get("X-Run-ID", "")
    step_id = request.headers.get("X-Step-ID", "")
    agent_name = request.headers.get("X-Agent-Name", "")
    capability = request.headers.get("X-Tool-Capability")
    claims = verify_tool_capability(
        capability,
        run_id=run_id,
        step_id=step_id,
        owner_id=owner_id,
        agent_name=agent_name,
    )
    if agent_name != card.name or claims is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A valid step-scoped capability is required.",
        )


def create_a2a_server(card: AgentCard, graph: Any) -> FastAPI:
    """Create an official A2A v1 server plus a development compatibility API."""
    settings = get_settings()
    task_store, engine = _task_store(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if engine is not None:
                await engine.dispose()

    app = FastAPI(title=f"A2A - {card.name}", version=card.version, lifespan=lifespan)
    executor = A2AAgentExecutor(card=card, graph=graph)
    protocol_card = card.to_protocol_card()
    handler = DefaultRequestHandler(
        agent_executor=A2AProtocolExecutor(executor),
        task_store=task_store,
        agent_card=protocol_card,
    )
    app.router.routes.extend(
        create_agent_card_routes(protocol_card, cache_control="public, max-age=60")
    )
    app.router.routes.extend(
        create_jsonrpc_routes(
            handler,
            "/a2a",
            context_builder=_A2ACallContextBuilder(),
            shutdown_grace_period=5,
        )
    )

    @app.middleware("http")
    async def authenticate_protocol_request(request: Request, call_next: Any) -> Any:
        if settings.require_authentication and request.url.path in {
            "/a2a",
            "/a2a/tasks",
            "/a2a/tasks/stream",
        }:
            try:
                _check_service_auth(request, card, settings)
            except HTTPException as exc:
                from fastapi.responses import JSONResponse

                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "agent": card.name}

    @app.get("/a2a/card", deprecated=True)
    async def compatibility_card() -> dict[str, Any]:
        from google.protobuf.json_format import MessageToDict

        return MessageToDict(protocol_card)

    @app.post("/a2a/tasks", response_model=A2ATaskResponse, deprecated=True)
    async def run_task(
        request: A2ATaskRequest,
        raw_request: Request,
        service_token: str | None = Header(default=None, alias="X-Internal-Service-Token"),
    ) -> A2ATaskResponse:
        del service_token
        if request.target_agent not in {card.name, card.name.replace("_agent", "")}:
            raise HTTPException(status_code=400, detail="Target agent mismatch")
        tracer = trace.get_tracer("agentic-a2a")
        extracted = TraceContextTextMapPropagator().extract(carrier=raw_request.headers)
        with tracer.start_as_current_span(f"a2a.agent.{card.name}", context=extracted) as span:
            span.set_attribute("a2a.caller", request.caller)
            span.set_attribute("a2a.task_id", request.task_id)
            return await executor.execute(request)

    @app.post("/a2a/tasks/stream", deprecated=True)
    async def run_task_stream(request: A2ATaskRequest) -> EventSourceResponse:
        if request.target_agent not in {card.name, card.name.replace("_agent", "")}:
            raise HTTPException(status_code=400, detail="Target agent mismatch")

        async def stream_generator() -> AsyncIterator[dict[str, str]]:
            async for event in executor.execute_stream(request):
                yield {
                    "event": str(event.get("event", "message")),
                    "data": json.dumps(event, ensure_ascii=False),
                }

        return EventSourceResponse(stream_generator())

    return app
