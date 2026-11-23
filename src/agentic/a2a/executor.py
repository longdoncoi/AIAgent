"""A2A AgentExecutor: Exposing LangGraph graphs over standardized A2A protocol."""

import hmac
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException, Request, status
from langchain_core.messages import HumanMessage
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from src.agentic.a2a.cards import AgentCard
from src.agentic.core.config import get_settings
from src.agentic.tools.execution_context import (
    ToolExecutionContext,
    reset_tool_execution_context,
    set_tool_execution_context,
)

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

    async def execute(self, request: A2ATaskRequest) -> A2ATaskResponse:
        """Executes a single step via the agent's internal LangGraph graph."""
        context_token = set_tool_execution_context(
            ToolExecutionContext(
                agent_name=self.card.name,
                step_id=request.task_id,
                hitl_approved=bool(request.metadata.get("hitl_approved", False)),
                owner_id=str(request.metadata.get("owner_id"))
                if request.metadata.get("owner_id")
                else None,
            )
        )
        try:
            instruction = request.payload.instruction
            context_str = ""
            if request.payload.context:
                context_str = f"\nAdditional Context: {request.payload.context}"

            initial_state = {"messages": [HumanMessage(content=instruction + context_str)]}

            result_state = await self.graph.ainvoke(initial_state)

            # Extract the last AI response
            messages = result_state.get("messages", [])
            output = ""
            for msg in reversed(messages):
                if hasattr(msg, "content") and msg.content and not isinstance(msg, HumanMessage):
                    output = str(msg.content)
                    break

            if not output and messages:
                output = str(messages[-1].content)

            return A2ATaskResponse(
                task_id=request.task_id,
                status="completed",
                output=output or "Task finished with empty response.",
            )
        except Exception as e:
            logger.error(f"Error executing A2A task in {self.card.name}: {e}", exc_info=True)
            return A2ATaskResponse(
                task_id=request.task_id,
                status="failed",
                output="",
                error=str(e),
            )
        finally:
            reset_tool_execution_context(context_token)

    async def execute_stream(self, request: A2ATaskRequest) -> AsyncIterator[dict[str, Any]]:
        """Streams tokens and execution lifecycle events from the sub-agent graph."""
        instruction = request.payload.instruction
        context_str = ""
        if request.payload.context:
            context_str = f"\nAdditional Context: {request.payload.context}"

        initial_state = {"messages": [HumanMessage(content=instruction + context_str)]}

        accumulated_output: list[str] = []
        context_token = set_tool_execution_context(
            ToolExecutionContext(
                agent_name=self.card.name,
                step_id=request.task_id,
                hitl_approved=bool(request.metadata.get("hitl_approved", False)),
                owner_id=str(request.metadata.get("owner_id"))
                if request.metadata.get("owner_id")
                else None,
            )
        )
        try:
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
            else:
                res = await self.execute(request)
                yield {
                    "event": "token",
                    "task_id": request.task_id,
                    "agent": self.card.name,
                    "token": res.output,
                }
                accumulated_output.append(res.output)

            yield {
                "event": "completed",
                "task_id": request.task_id,
                "agent": self.card.name,
                "output": "".join(accumulated_output),
            }
        except Exception as e:
            logger.error(f"Error in streaming A2A task in {self.card.name}: {e}", exc_info=True)
            yield {
                "event": "error",
                "task_id": request.task_id,
                "agent": self.card.name,
                "error": str(e),
            }
        finally:
            reset_tool_execution_context(context_token)


def create_a2a_server(card: AgentCard, graph: Any) -> FastAPI:
    """Creates a FastAPI app implementing the A2A protocol endpoints."""
    app = FastAPI(title=f"A2A - {card.name}", version=card.version)
    executor = A2AAgentExecutor(card=card, graph=graph)
    settings = get_settings()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "agent": card.name}

    @app.get("/a2a/card")
    async def get_card() -> AgentCard:
        return card

    @app.post("/a2a/tasks", response_model=A2ATaskResponse)
    async def run_task(
        request: A2ATaskRequest,
        raw_request: Request,
        service_token: str | None = Header(default=None, alias="X-Internal-Service-Token"),
    ) -> A2ATaskResponse:
        # S2S Authentication check
        if settings.require_authentication:
            if not service_token or not hmac.compare_digest(
                service_token, settings.s2s_internal_token
            ):
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
        if settings.require_authentication:
            if not service_token or not hmac.compare_digest(
                service_token, settings.s2s_internal_token
            ):
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

        async def _stream_generator() -> AsyncIterator[dict[str, str]]:
            async for ev in executor.execute_stream(request):
                yield {
                    "event": str(ev.get("event", "message")),
                    "data": json.dumps(ev, ensure_ascii=False),
                }

        return EventSourceResponse(_stream_generator())

    return app
