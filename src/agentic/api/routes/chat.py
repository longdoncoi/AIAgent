"""Chat API endpoints supporting standard JSON and SSE Streaming."""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from langchain_core.messages import HumanMessage
from sse_starlette.sse import EventSourceResponse

from src.agentic.api.deps import get_current_user, get_graph, get_guardrails_dep
from src.agentic.core.errors import GuardrailViolation
from src.agentic.core.schemas import ChatRequest, ChatResponse, StepResult
from src.agentic.safety.guardrails import GuardrailManager, StreamingOutputGuard

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/chat", tags=["Chat"])


@router.post("", response_model=ChatResponse)
async def chat_endpoint(
    req: ChatRequest,
    graph: Any = Depends(get_graph),
    guardrails: GuardrailManager = Depends(get_guardrails_dep),
    user: dict = Depends(get_current_user),
) -> ChatResponse:
    """Standard non-streaming chat endpoint."""
    thread_id = req.thread_id or str(uuid.uuid4())

    try:
        clean_query = guardrails.sanitize_input(req.query)
    except GuardrailViolation as gv:
        raise HTTPException(
            status_code=400,
            detail=f"{gv.message} (rule: {gv.rule})",
        )

    config = {"configurable": {"thread_id": thread_id}}
    initial_input = {
        "messages": [HumanMessage(content=clean_query)],
        "thread_id": thread_id,
        "metadata": {**req.metadata, "user_id": user.get("user_id")},
    }

    try:
        final_state = await graph.ainvoke(initial_input, config=config)
    except Exception as e:
        logger.error(f"Execution error on thread {thread_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Agent execution failed: {str(e)}")

    step_results: list[StepResult] = []
    requires_approval = False
    pending_step = None

    if final_state.get("plan") and final_state["plan"].steps:
        for s in final_state["plan"].steps:
            step_results.append(
                StepResult(
                    step_id=s.id,
                    agent=s.agent,
                    output=s.output or "",
                    status=s.status,
                )
            )

    # Check if graph paused on interrupt
    state_snapshot = graph.get_state(config)
    if state_snapshot.next:
        # Graph was interrupted
        requires_approval = True
        if state_snapshot.tasks:
            interrupts = state_snapshot.tasks[0].interrupts
            if interrupts:
                pending_step = str(interrupts[0].value)

    return ChatResponse(
        thread_id=thread_id,
        answer=final_state.get("final_answer") or (
            "Execution paused waiting for Human-In-The-Loop approval." if requires_approval else "Completed."
        ),
        steps=step_results,
        requires_approval=requires_approval,
        pending_approval_step=pending_step,
        metadata={"user_id": user.get("user_id")},
    )


@router.post("/stream")
async def chat_stream_endpoint(
    req: ChatRequest,
    graph: Any = Depends(get_graph),
    guardrails: GuardrailManager = Depends(get_guardrails_dep),
    user: dict = Depends(get_current_user),
) -> EventSourceResponse:
    """Server-Sent Events (SSE) streaming endpoint."""
    thread_id = req.thread_id or str(uuid.uuid4())

    try:
        clean_query = guardrails.sanitize_input(req.query)
    except GuardrailViolation as gv:
        gv_msg = str(gv)
        async def err_gen() -> AsyncIterator[dict]:
            yield {
                "event": "error",
                "data": json.dumps({"error": gv_msg, "type": "GuardrailViolation"}),
            }
        return EventSourceResponse(err_gen())

    config = {"configurable": {"thread_id": thread_id}}
    initial_input = {
        "messages": [HumanMessage(content=clean_query)],
        "thread_id": thread_id,
        "metadata": {**req.metadata, "user_id": user.get("user_id")},
    }

    async def event_generator() -> AsyncIterator[dict]:
        stream_guard = StreamingOutputGuard(manager=guardrails)
        from opentelemetry import trace as otel_trace
        current_span = otel_trace.get_current_span()
        span_ctx = current_span.get_span_context() if current_span else None
        active_trace_id = format(span_ctx.trace_id, "032x") if span_ctx and span_ctx.is_valid else uuid.uuid4().hex
        active_span_id = format(span_ctx.span_id, "016x") if span_ctx and span_ctx.is_valid else uuid.uuid4().hex[:16]

        try:
            # Yield initial thread event
            yield {
                "event": "start",
                "data": json.dumps({"thread_id": thread_id, "query": clean_query, "trace_id": active_trace_id}),
            }

            async for event in graph.astream_events(initial_input, config=config, version="v2"):
                kind = event.get("event")
                node_name = event.get("metadata", {}).get("langgraph_node")

                if kind == "on_chain_end" and node_name == "planner":
                    output = event.get("data", {}).get("output", {})
                    plan = output.get("plan")
                    if plan:
                        yield {
                            "event": "plan",
                            "data": json.dumps({
                                "plan": plan.model_dump() if hasattr(plan, "model_dump") else plan,
                                "trace_id": active_trace_id,
                            }),
                        }

                elif kind == "on_chain_end" and node_name == "dispatch":
                    output = event.get("data", {}).get("output", {})
                    yield {
                        "event": "step_complete",
                        "data": json.dumps({
                            "step_index": output.get("current_step_index"),
                            "trace_id": active_trace_id,
                        }),
                    }

                elif kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        token_content = str(chunk.content)
                        clean_token = stream_guard.feed(token_content)
                        if clean_token:
                            yield {
                                "event": "token",
                                "data": json.dumps({
                                    "token": clean_token,
                                    "node": node_name or "llm",
                                    "trace_id": active_trace_id,
                                    "span_id": active_span_id,
                                }),
                            }
                        if stream_guard.safety_flags:
                            flag = stream_guard.safety_flags.pop(0)
                            yield {
                                "event": "safety_flag",
                                "data": json.dumps({**flag, "trace_id": active_trace_id}),
                            }

                elif kind == "on_chain_end" and node_name == "finalize":
                    flushed = stream_guard.flush()
                    if flushed:
                        yield {
                            "event": "token",
                            "data": json.dumps({
                                "token": flushed,
                                "node": "llm",
                                "trace_id": active_trace_id,
                                "span_id": active_span_id,
                            }),
                        }
                    output = event.get("data", {}).get("output", {})
                    final_ans = output.get("final_answer")
                    if final_ans:
                        yield {
                            "event": "final",
                            "data": json.dumps({
                                "final_answer": final_ans,
                                "thread_id": thread_id,
                                "trace_id": active_trace_id,
                                "span_id": active_span_id,
                            }),
                        }

            # Flush any remaining buffer if stream finished
            final_flushed = stream_guard.flush()
            if final_flushed:
                yield {
                    "event": "token",
                    "data": json.dumps({
                        "token": final_flushed,
                        "node": "llm",
                        "trace_id": active_trace_id,
                        "span_id": active_span_id,
                    }),
                }

            # Check if execution paused waiting for HITL approval
            state_snapshot = graph.get_state(config)
            if state_snapshot.next:
                tasks = state_snapshot.tasks
                interrupt_val = tasks[0].interrupts[0].value if tasks and tasks[0].interrupts else "High-risk action"
                yield {
                    "event": "hitl_required",
                    "data": json.dumps({
                        "thread_id": thread_id,
                        "interrupt": interrupt_val,
                        "resume_url": f"/api/v1/approvals/{thread_id}/resume",
                        "trace_id": active_trace_id,
                    }),
                }

        except (asyncio.CancelledError, GeneratorExit):
            logger.info(f"SSE stream cancelled or closed by client on thread {thread_id}.")
            raise
        except Exception as e:
            logger.error(f"SSE stream error: {e}", exc_info=True)
            yield {
                "event": "error",
                "data": json.dumps({"error": str(e), "trace_id": active_trace_id}),
            }

    return EventSourceResponse(event_generator())
