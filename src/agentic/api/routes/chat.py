"""Chat API endpoints supporting standard JSON and SSE Streaming."""

import asyncio
import inspect
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse

from src.agentic.api.deps import get_current_user, get_graph, get_guardrails_dep
from src.agentic.core.config import get_settings
from src.agentic.core.errors import GuardrailViolation
from src.agentic.core.execution_context import (
    ToolExecutionContext,
    reset_tool_execution_context,
    set_tool_execution_context,
)
from src.agentic.core.schemas import ChatRequest, ChatResponse, StepResult
from src.agentic.memory.thread_registry import (
    ThreadOwnershipError,
    ThreadRegistryUnavailable,
    get_thread_registry,
)
from src.agentic.observability.metrics import TTFT_DURATION
from src.agentic.orchestrator.state import build_turn_input
from src.agentic.safety.guardrails import GuardrailManager, StreamingOutputGuard

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/chat", tags=["Chat"])


async def _graph_state(graph: Any, config: dict[str, Any]) -> Any:
    """Use async state I/O while supporting legacy graph test doubles."""
    candidate = graph.aget_state(config)
    if inspect.isawaitable(candidate):
        return await candidate
    return graph.get_state(config)


@router.post("", response_model=ChatResponse)
async def chat_endpoint(
    req: ChatRequest,
    graph: Any = Depends(get_graph),
    guardrails: GuardrailManager = Depends(get_guardrails_dep),
    user: dict[str, Any] = Depends(get_current_user),
) -> ChatResponse:
    """Standard non-streaming chat endpoint."""
    thread_id = req.thread_id or str(uuid.uuid4())
    owner_id = str(user.get("user_id") or "anonymous_user")
    default_tenant = get_settings().rag_default_tenant_id
    tenant_id = str(user.get("tenant_id") or default_tenant)
    try:
        checkpoint_thread_id = await get_thread_registry().claim(thread_id, owner_id, tenant_id)
    except ThreadOwnershipError as exc:
        raise HTTPException(status_code=403, detail="Thread is owned by another principal.") from exc
    except ThreadRegistryUnavailable as exc:
        raise HTTPException(status_code=503, detail="Thread registry is unavailable.") from exc

    try:
        # Full guardrail (regex + semantic) plus structured metadata sanitization.
        clean_query = await guardrails.asanitize_input(req.query)
        safe_metadata = await asyncio.to_thread(guardrails.sanitize_context, req.metadata)
    except GuardrailViolation as gv:
        raise HTTPException(
            status_code=400,
            detail=f"{gv.message} (rule: {gv.rule})",
        ) from gv

    config = {"configurable": {"thread_id": checkpoint_thread_id}}
    initial_input = build_turn_input(
        user_query=clean_query,
        thread_id=thread_id,
        metadata={
            **safe_metadata,
            "user_id": owner_id,
            "public_thread_id": thread_id,
            "user_roles": list(user.get("roles") or [user.get("role", "guest")]),
            "department": str(user.get("department") or "public"),
            "client_id": str(user.get("client_id") or "unattributed"),
            "tenant_id": tenant_id,
        },
    )

    allocation_token = set_tool_execution_context(
        ToolExecutionContext(
            agent_name="supervisor",
            step_id="orchestration",
            owner_id=owner_id,
            client_id=str(user.get("client_id") or "unattributed"),
            department=str(user.get("department") or "public"),
            tenant_id=tenant_id,
        )
    )
    try:
        final_state = await graph.ainvoke(initial_input, config=config)
    except Exception as e:
        logger.error(f"Execution error on thread {thread_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Agent execution failed.") from e
    finally:
        reset_tool_execution_context(allocation_token)

    step_results: list[StepResult] = []
    requires_approval = False
    pending_step = None

    if final_state.get("plan") and final_state["plan"].steps:
        for s in final_state["plan"].steps:
            step_results.append(
                StepResult(
                    step_id=s.id,
                    agent=s.agent,
                    output=await asyncio.to_thread(guardrails.sanitize_output, s.output or ""),
                    status=s.public_status,
                )
            )

    # Check if graph paused on interrupt
    state_snapshot = await _graph_state(graph, config)
    if state_snapshot.next:
        # Graph was interrupted
        requires_approval = True
        if state_snapshot.tasks:
            interrupts = state_snapshot.tasks[0].interrupts
            if interrupts:
                interrupt_value = interrupts[0].value
                pending_step = (
                    str(interrupt_value.get("step_id"))
                    if isinstance(interrupt_value, dict) and interrupt_value.get("step_id")
                    else str(interrupt_value)
                )

    final_answer = final_state.get("final_answer")
    if final_answer:
        final_answer = await asyncio.to_thread(guardrails.sanitize_output, str(final_answer))

    return ChatResponse(
        thread_id=thread_id,
        answer=final_answer
        or (
            "Execution paused waiting for Human-In-The-Loop approval."
            if requires_approval
            else "Completed."
        ),
        steps=step_results,
        requires_approval=requires_approval,
        pending_approval_step=pending_step,
        metadata={
            "user_id": user.get("user_id"),
            "ai_generated": True,
            "disclaimer": "AI-generated response. Verify critical decisions and cited sources.",
        },
    )


@router.post("/stream")
async def chat_stream_endpoint(
    req: ChatRequest,
    graph: Any = Depends(get_graph),
    guardrails: GuardrailManager = Depends(get_guardrails_dep),
    user: dict[str, Any] = Depends(get_current_user),
) -> EventSourceResponse:
    """Server-Sent Events (SSE) streaming endpoint."""
    thread_id = req.thread_id or str(uuid.uuid4())
    owner_id = str(user.get("user_id") or "anonymous_user")
    default_tenant = get_settings().rag_default_tenant_id
    tenant_id = str(user.get("tenant_id") or default_tenant)
    try:
        checkpoint_thread_id = await get_thread_registry().claim(thread_id, owner_id, tenant_id)
    except ThreadOwnershipError:
        checkpoint_thread_id = ""
        ownership_error = "Thread is owned by another principal."
    except ThreadRegistryUnavailable:
        checkpoint_thread_id = ""
        ownership_error = "Thread registry is unavailable."
    else:
        ownership_error = ""

    try:
        clean_query = await guardrails.asanitize_input(req.query)
        safe_metadata = await asyncio.to_thread(guardrails.sanitize_context, req.metadata)
    except GuardrailViolation as gv:
        gv_msg = str(gv)

        async def err_gen() -> AsyncIterator[dict[str, Any]]:
            yield {
                "event": "error",
                "data": json.dumps({"error": gv_msg, "type": "GuardrailViolation"}),
            }

        return EventSourceResponse(err_gen())

    config = {"configurable": {"thread_id": checkpoint_thread_id}}
    initial_input = build_turn_input(
        user_query=clean_query,
        thread_id=thread_id,
        metadata={
            **safe_metadata,
            "user_id": owner_id,
            "public_thread_id": thread_id,
            "user_roles": list(user.get("roles") or [user.get("role", "guest")]),
            "department": str(user.get("department") or "public"),
            "client_id": str(user.get("client_id") or "unattributed"),
            "tenant_id": tenant_id,
        },
    )

    async def event_generator() -> AsyncIterator[dict[str, Any]]:
        allocation_token = set_tool_execution_context(
            ToolExecutionContext(
                agent_name="supervisor",
                step_id="orchestration",
                owner_id=owner_id,
                client_id=str(user.get("client_id") or "unattributed"),
                department=str(user.get("department") or "public"),
                tenant_id=tenant_id,
            )
        )
        stream_guard = StreamingOutputGuard(manager=guardrails)
        stream_start_time = time.perf_counter()
        first_token_emitted = False
        saw_rag_step = False
        buffered_rag_tokens: list[tuple[str, str]] = []
        from opentelemetry import trace as otel_trace

        current_span = otel_trace.get_current_span()
        span_ctx = current_span.get_span_context() if current_span else None
        active_trace_id = (
            format(span_ctx.trace_id, "032x")
            if span_ctx and span_ctx.is_valid
            else uuid.uuid4().hex
        )
        active_span_id = (
            format(span_ctx.span_id, "016x")
            if span_ctx and span_ctx.is_valid
            else uuid.uuid4().hex[:16]
        )

        try:
            if ownership_error:
                yield {
                    "event": "error",
                    "data": json.dumps({"error": ownership_error, "trace_id": active_trace_id}),
                }
                return
            # Yield initial thread event
            yield {
                "event": "start",
                "data": json.dumps(
                    {"thread_id": thread_id, "query": clean_query, "trace_id": active_trace_id}
                ),
            }

            async for event in graph.astream_events(initial_input, config=config, version="v2"):
                kind = event.get("event")
                node_name = event.get("metadata", {}).get("langgraph_node") or event.get("name")

                if kind == "on_chain_end" and node_name == "planner":
                    output = event.get("data", {}).get("output", {})
                    plan = output.get("plan")
                    if plan:
                        if any(getattr(s, "agent", None) == "rag" for s in getattr(plan, "steps", [])):
                            saw_rag_step = True
                        yield {
                            "event": "plan",
                            "data": json.dumps(
                                {
                                    "plan": plan.model_dump()
                                    if hasattr(plan, "model_dump")
                                    else plan,
                                    "trace_id": active_trace_id,
                                }
                            ),
                        }

                elif kind == "on_chain_end" and node_name == "dispatch":
                    output = event.get("data", {}).get("output", {})
                    yield {
                        "event": "step_complete",
                        "data": json.dumps(
                            {
                                "step_index": output.get("current_step_index"),
                                "trace_id": active_trace_id,
                            }
                        ),
                    }

                elif kind == "on_chat_model_stream" and node_name == "finalize":
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        token_content = str(chunk.content)
                        clean_token = stream_guard.feed(token_content)
                        if clean_token:
                            if saw_rag_step:
                                # Buffer RAG synthesis tokens until finalize validates citations.
                                buffered_rag_tokens.append((clean_token, node_name or "llm"))
                            else:
                                if not first_token_emitted:
                                    first_token_emitted = True
                                    TTFT_DURATION.labels(endpoint="chat_stream").observe(
                                        time.perf_counter() - stream_start_time
                                    )
                                yield {
                                    "event": "token",
                                    "data": json.dumps(
                                        {
                                            "token": clean_token,
                                            "node": node_name or "llm",
                                            "trace_id": active_trace_id,
                                            "span_id": active_span_id,
                                        }
                                    ),
                                }
                        if stream_guard.safety_flags:
                            flag = stream_guard.safety_flags.pop(0)
                            yield {
                                "event": "safety_flag",
                                "data": json.dumps({**flag, "trace_id": active_trace_id}),
                            }

                elif kind == "on_chat_model_stream":
                    # Do not leak planner/sub-agent draft tokens; expose only lifecycle progress.
                    yield {
                        "event": "step_progress",
                        "data": json.dumps(
                            {"node": node_name or "agent", "trace_id": active_trace_id}
                        ),
                    }

                elif kind == "on_chain_end" and node_name in {"finalize", "fast_finalize"}:
                    flushed = stream_guard.flush()
                    output = event.get("data", {}).get("output", {})
                    final_ans = output.get("final_answer")
                    sanitized_final = (
                        await asyncio.to_thread(guardrails.sanitize_output, str(final_ans))
                        if final_ans
                        else None
                    )
                    if node_name == "fast_finalize" and sanitized_final:
                        # The conversational fast path returns an already-composed
                        # answer and intentionally performs no second LLM streaming
                        # call. Emit it as one safe token so SSE clients receive the
                        # same token/final contract as the regular finalize path.
                        if not first_token_emitted:
                            first_token_emitted = True
                            TTFT_DURATION.labels(endpoint="chat_stream").observe(
                                time.perf_counter() - stream_start_time
                            )
                        yield {
                            "event": "token",
                            "data": json.dumps(
                                {
                                    "token": sanitized_final,
                                    "node": "fast_finalize",
                                    "trace_id": active_trace_id,
                                    "span_id": active_span_id,
                                }
                            ),
                        }
                    elif saw_rag_step and sanitized_final and sanitized_final.startswith("**Manual Review Required**"):
                        buffered_rag_tokens.clear()
                        if not first_token_emitted:
                            first_token_emitted = True
                            TTFT_DURATION.labels(endpoint="chat_stream").observe(
                                time.perf_counter() - stream_start_time
                            )
                        yield {
                            "event": "token",
                            "data": json.dumps(
                                {
                                    "token": sanitized_final,
                                    "node": "finalize",
                                    "trace_id": active_trace_id,
                                    "span_id": active_span_id,
                                }
                            ),
                        }
                    else:
                        for tok, tok_node in buffered_rag_tokens:
                            if not first_token_emitted:
                                first_token_emitted = True
                                TTFT_DURATION.labels(endpoint="chat_stream").observe(
                                    time.perf_counter() - stream_start_time
                                )
                            yield {
                                "event": "token",
                                "data": json.dumps(
                                    {
                                        "token": tok,
                                        "node": tok_node,
                                        "trace_id": active_trace_id,
                                        "span_id": active_span_id,
                                    }
                                ),
                            }
                        buffered_rag_tokens.clear()
                        if flushed:
                            if not first_token_emitted:
                                first_token_emitted = True
                                TTFT_DURATION.labels(endpoint="chat_stream").observe(
                                    time.perf_counter() - stream_start_time
                                )
                            yield {
                                "event": "token",
                                "data": json.dumps(
                                    {
                                        "token": flushed,
                                        "node": "llm",
                                        "trace_id": active_trace_id,
                                        "span_id": active_span_id,
                                    }
                                ),
                            }
                    if sanitized_final:
                        yield {
                            "event": "final",
                            "data": json.dumps(
                                {
                                    "final_answer": sanitized_final,
                                    "thread_id": thread_id,
                                    "trace_id": active_trace_id,
                                    "span_id": active_span_id,
                                    "metadata": {
                                        "ai_generated": True,
                                        "disclaimer": "AI-generated response. Verify critical decisions and cited sources.",
                                    },
                                }
                            ),
                        }

            # Flush any remaining buffer if stream finished
            final_flushed = stream_guard.flush()
            if final_flushed:
                yield {
                    "event": "token",
                    "data": json.dumps(
                        {
                            "token": final_flushed,
                            "node": "llm",
                            "trace_id": active_trace_id,
                            "span_id": active_span_id,
                        }
                    ),
                }

            # Check if execution paused waiting for HITL approval
            state_snapshot = await _graph_state(graph, config)
            if state_snapshot.next:
                tasks = state_snapshot.tasks
                interrupt_val = (
                    tasks[0].interrupts[0].value
                    if tasks and tasks[0].interrupts
                    else "High-risk action"
                )
                safe_interrupt = await asyncio.to_thread(
                    guardrails.sanitize_output, json.dumps(interrupt_val, default=str)
                )
                yield {
                    "event": "hitl_required",
                    "data": json.dumps(
                        {
                            "thread_id": thread_id,
                            "interrupt": safe_interrupt,
                            "resume_url": f"/api/v1/approvals/{thread_id}/resume",
                            "trace_id": active_trace_id,
                        }
                    ),
                }

        except (asyncio.CancelledError, GeneratorExit):
            logger.info(f"SSE stream cancelled or closed by client on thread {thread_id}.")
            raise
        except Exception as e:
            logger.error(f"SSE stream error: {e}", exc_info=True)
            yield {
                "event": "error",
                "data": json.dumps(
                    {"error": "Agent stream failed.", "trace_id": active_trace_id}
                ),
            }
        finally:
            reset_tool_execution_context(allocation_token)

    return EventSourceResponse(event_generator())
