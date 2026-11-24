"""Supervisor client for the official A2A v1 protocol."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any

import httpx
from a2a.client import ClientCallContext, ClientConfig, create_client
from a2a.helpers import get_stream_response_text, new_text_message
from a2a.types import Role, SendMessageRequest, StreamResponse, TaskState
from a2a.utils.errors import A2AError
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from src.agentic.a2a.cards import AgentCard, get_agent_card
from src.agentic.a2a.executor import A2ATaskPayload, A2ATaskRequest, A2ATaskResponse
from src.agentic.core.config import get_settings
from src.agentic.core.errors import A2ACommunicationError
from src.agentic.observability.metrics import A2A_REQUESTS_TOTAL

logger = logging.getLogger(__name__)


class A2AClient:
    """Pooled A2A client; local execution is available only outside production."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._local_executors: dict[str, Any] = {}
        self._http_client: httpx.AsyncClient | None = None
        self._concurrency_semaphore = asyncio.Semaphore(self.settings.a2a_max_concurrent_requests)

    def _get_http_client(self, timeout: float = 60.0) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            limits = httpx.Limits(
                max_keepalive_connections=50, max_connections=200, keepalive_expiry=30.0
            )
            kwargs: dict[str, Any] = {"limits": limits, "timeout": timeout}
            if (
                self.settings.enable_mtls
                and self.settings.mtls_cert_path
                and self.settings.mtls_key_path
            ):
                kwargs["cert"] = (self.settings.mtls_cert_path, self.settings.mtls_key_path)
                if self.settings.mtls_ca_cert_path:
                    kwargs["verify"] = self.settings.mtls_ca_cert_path
            self._http_client = httpx.AsyncClient(**kwargs)
        return self._http_client

    async def aclose(self) -> None:
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

    def _get_local_graph(self, agent_name: str) -> Any:
        norm_name = agent_name.replace("_agent", "")
        if norm_name not in self._local_executors:
            if norm_name == "chat":
                from src.agentic.agents.chat.graph import get_chat_graph

                self._local_executors[norm_name] = get_chat_graph()
            elif norm_name == "rag":
                from src.agentic.agents.rag.graph import get_rag_graph

                self._local_executors[norm_name] = get_rag_graph()
            elif norm_name == "coding":
                from src.agentic.agents.coding.graph import get_coding_graph

                self._local_executors[norm_name] = get_coding_graph()
        return self._local_executors.get(norm_name)

    def _request_parts(
        self,
        card: AgentCard,
        step_id: str,
        instruction: str,
        context: dict[str, Any],
        metadata: dict[str, Any],
        timeout: float,
    ) -> tuple[SendMessageRequest, ClientCallContext]:
        owner_id = str(metadata.get("owner_id") or "")
        run_id = str(metadata.get("run_id") or "")
        capability = str(metadata.get("approval_capability") or "")
        if self.settings.require_authentication and not all(
            (owner_id, run_id, capability, step_id)
        ):
            raise A2ACommunicationError(
                target_agent=card.name,
                message="Authenticated A2A calls require owner, run, step, and capability bindings.",
            )
        wire_metadata = dict(metadata)
        wire_metadata["context"] = context
        wire_metadata["step_id"] = step_id
        request = SendMessageRequest(
            message=new_text_message(
                instruction,
                context_id=run_id or None,
                role=Role.ROLE_USER,
            ),
            metadata=wire_metadata,
        )
        headers = {
            "X-Internal-Service-Token": self.settings.s2s_internal_token,
            "X-Owner-ID": owner_id,
            "X-Run-ID": run_id,
            "X-Step-ID": step_id,
            "X-Agent-Name": card.name,
            "X-Tool-Capability": capability,
        }
        current_span = trace.get_current_span()
        if current_span.is_recording():
            TraceContextTextMapPropagator().inject(headers)
        return request, ClientCallContext(timeout=timeout, service_parameters=headers)

    async def _remote_stream(
        self,
        card: AgentCard,
        request: SendMessageRequest,
        call_context: ClientCallContext,
        timeout: float,
    ) -> AsyncIterator[StreamResponse]:
        http_client = self._get_http_client(timeout)
        protocol_client = await create_client(
            card.to_protocol_card(),
            ClientConfig(streaming=True, httpx_client=http_client),
        )
        async for event in protocol_client.send_message(request, context=call_context):
            yield event

    async def _retrying_remote_stream(
        self,
        target_agent: str,
        card: AgentCard,
        request: SendMessageRequest,
        call_context: ClientCallContext,
        timeout: float,
    ) -> AsyncIterator[StreamResponse]:
        attempts = max(1, self.settings.a2a_max_retries)
        for attempt in range(attempts):
            try:
                async with self._concurrency_semaphore:
                    events = [
                        event
                        async for event in self._remote_stream(card, request, call_context, timeout)
                    ]
                for event in events:
                    yield event
                return
            except (A2AError, httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt == attempts - 1:
                    raise A2ACommunicationError(
                        target_agent=target_agent,
                        message="A2A transport is unavailable or rejected the request.",
                    ) from exc
                backoff = min(
                    self.settings.a2a_retry_backoff_max_seconds,
                    self.settings.a2a_retry_backoff_base_seconds * (2**attempt),
                ) * random.uniform(0.5, 1.0)
                logger.warning(
                    "A2A request to %s failed; retrying in %.2fs (%s/%s)",
                    target_agent,
                    backoff,
                    attempt + 1,
                    attempts,
                )
                await asyncio.sleep(backoff)

    async def call_agent(
        self,
        target_agent: str,
        step_id: str,
        instruction: str,
        context: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        timeout: float = 60.0,
    ) -> A2ATaskResponse:
        card = get_agent_card(target_agent)
        if card is not None:
            request, call_context = self._request_parts(
                card, step_id, instruction, context or {}, metadata or {}, timeout
            )
            output = ""
            error: str | None = None
            failed = False
            try:
                async for event in self._retrying_remote_stream(
                    target_agent, card, request, call_context, timeout
                ):
                    text = get_stream_response_text(event)
                    if event.HasField("artifact_update") and text:
                        output += text
                    elif event.HasField("task") and text:
                        output = text
                    if event.HasField("status_update"):
                        state = event.status_update.status.state
                        if state in {
                            TaskState.TASK_STATE_FAILED,
                            TaskState.TASK_STATE_CANCELED,
                            TaskState.TASK_STATE_REJECTED,
                            TaskState.TASK_STATE_AUTH_REQUIRED,
                        }:
                            failed = True
                            error = text or "Agent task failed."
                A2A_REQUESTS_TOTAL.labels(
                    caller="supervisor",
                    target_agent=target_agent,
                    status="error" if failed else "success",
                ).inc()
                return A2ATaskResponse(
                    task_id=step_id,
                    status="failed" if failed else "completed",
                    output=output,
                    error=error,
                )
            except A2ACommunicationError:
                if not self.settings.allow_local_agent_fallback:
                    raise
                logger.info("A2A unavailable; using development-only local fallback.")
        return await self._call_local(
            target_agent, step_id, instruction, context or {}, metadata or {}
        )

    async def _call_local(
        self,
        target_agent: str,
        step_id: str,
        instruction: str,
        context: dict[str, Any],
        metadata: dict[str, Any],
    ) -> A2ATaskResponse:
        local_graph = self._get_local_graph(target_agent)
        card = get_agent_card(target_agent)
        if not local_graph:
            raise A2ACommunicationError(
                target_agent=target_agent,
                message=f"Sub-agent '{target_agent}' is not registered and local fallback failed.",
            )
        if not card:
            raise A2ACommunicationError(
                target_agent=target_agent,
                message=f"AgentCard for '{target_agent}' not found.",
            )
        from src.agentic.a2a.executor import A2AAgentExecutor

        response = await A2AAgentExecutor(card, local_graph).execute(
            A2ATaskRequest(
                task_id=step_id,
                target_agent=target_agent,
                payload=A2ATaskPayload(instruction=instruction, context=context),
                metadata=metadata,
            )
        )
        A2A_REQUESTS_TOTAL.labels(
            caller="supervisor",
            target_agent=target_agent,
            status="success" if response.status == "completed" else "error",
        ).inc()
        return response

    async def call_agent_stream(
        self,
        target_agent: str,
        step_id: str,
        instruction: str,
        context: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        timeout: float = 60.0,
    ) -> AsyncIterator[dict[str, Any]]:
        card = get_agent_card(target_agent)
        if card is not None:
            request, call_context = self._request_parts(
                card, step_id, instruction, context or {}, metadata or {}, timeout
            )
            output: list[str] = []
            try:
                yield {"event": "start", "task_id": step_id, "agent": card.name}
                async for response in self._retrying_remote_stream(
                    target_agent, card, request, call_context, timeout
                ):
                    text = get_stream_response_text(response)
                    if response.HasField("artifact_update") and text:
                        output.append(text)
                        yield {
                            "event": "token",
                            "task_id": step_id,
                            "agent": card.name,
                            "token": text,
                        }
                    if response.HasField("status_update"):
                        state = response.status_update.status.state
                        if state in {
                            TaskState.TASK_STATE_FAILED,
                            TaskState.TASK_STATE_CANCELED,
                            TaskState.TASK_STATE_REJECTED,
                            TaskState.TASK_STATE_AUTH_REQUIRED,
                        }:
                            yield {
                                "event": "error",
                                "task_id": step_id,
                                "agent": card.name,
                                "error": text or "Agent task failed.",
                            }
                            return
                A2A_REQUESTS_TOTAL.labels(
                    caller="supervisor", target_agent=target_agent, status="success"
                ).inc()
                yield {
                    "event": "completed",
                    "task_id": step_id,
                    "agent": card.name,
                    "output": "".join(output),
                }
                return
            except A2ACommunicationError:
                if not self.settings.allow_local_agent_fallback:
                    raise
                logger.info("A2A stream unavailable; using development-only local fallback.")

        local_graph = self._get_local_graph(target_agent)
        card = get_agent_card(target_agent)
        if not local_graph or not card:
            raise A2ACommunicationError(
                target_agent=target_agent,
                message=f"Sub-agent '{target_agent}' local streaming fallback unavailable.",
            )
        from src.agentic.a2a.executor import A2AAgentExecutor

        local_request = A2ATaskRequest(
            task_id=step_id,
            target_agent=target_agent,
            payload=A2ATaskPayload(instruction=instruction, context=context or {}),
            metadata=metadata or {},
        )
        async for event in A2AAgentExecutor(card, local_graph).execute_stream(local_request):
            yield event


@lru_cache
def get_a2a_client() -> A2AClient:
    return A2AClient()
