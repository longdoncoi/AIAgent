"""A2A Client used by Supervisor to dispatch tasks to Sub-Agents."""

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any

import httpx
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from src.agentic.a2a.cards import get_agent_card
from src.agentic.a2a.executor import A2ATaskPayload, A2ATaskRequest, A2ATaskResponse
from src.agentic.core.config import get_settings
from src.agentic.core.errors import A2ACommunicationError
from src.agentic.observability.metrics import A2A_REQUESTS_TOTAL

logger = logging.getLogger(__name__)


class A2AClient:
    """Client for calling sub-agents over A2A protocol with fallback to direct in-process execution."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._endpoints: dict[str, str] = {
            "chat": self.settings.a2a_chat_agent_url,
            "chat_agent": self.settings.a2a_chat_agent_url,
            "rag": self.settings.a2a_rag_agent_url,
            "rag_agent": self.settings.a2a_rag_agent_url,
            "coding": self.settings.a2a_coding_agent_url,
            "coding_agent": self.settings.a2a_coding_agent_url,
        }
        self._local_executors: dict[str, Any] = {}
        self._http_client: httpx.AsyncClient | None = None
        self._concurrency_semaphore = asyncio.Semaphore(self.settings.a2a_max_concurrent_requests)

    def _get_http_client(self, timeout: float = 60.0) -> httpx.AsyncClient:
        """Returns or creates the pooled, persistent HTTP client with optional mTLS support."""
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
        """Gracefully closes persistent HTTP client connection pool."""
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

    def _get_local_graph(self, agent_name: str) -> Any:
        """Lazy-loads sub-agent graph for in-process fallback."""
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

    async def call_agent(
        self,
        target_agent: str,
        step_id: str,
        instruction: str,
        context: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        timeout: float = 60.0,
    ) -> A2ATaskResponse:
        """Calls a sub-agent over A2A HTTP, falling back to local graph execution if server is offline."""
        base_url = self._endpoints.get(target_agent)
        endpoint = f"{base_url}/tasks" if base_url else None

        req = A2ATaskRequest(
            task_id=step_id,
            caller="supervisor",
            target_agent=target_agent,
            payload=A2ATaskPayload(instruction=instruction, context=context or {}),
            metadata=metadata or {},
        )

        headers: dict[str, str] = {
            "X-Internal-Service-Token": self.settings.s2s_internal_token,
        }
        # Inject OpenTelemetry tracing header
        current_span = trace.get_current_span()
        if current_span.is_recording():
            TraceContextTextMapPropagator().inject(headers)

        if endpoint:
            max_attempts = max(1, self.settings.a2a_max_retries)
            for attempt in range(max_attempts):
                try:
                    async with self._concurrency_semaphore:
                        client = self._get_http_client(timeout=timeout)
                        resp = await client.post(
                            endpoint, json=req.model_dump(), headers=headers, timeout=timeout
                        )
                    if resp.status_code == 200:
                        A2A_REQUESTS_TOTAL.labels(
                            caller="supervisor", target_agent=target_agent, status="success"
                        ).inc()
                        return A2ATaskResponse(**resp.json())
                    if (
                        resp.status_code in {429, 502, 503, 504}
                        and attempt < max_attempts - 1
                    ):
                        backoff = min(
                            self.settings.a2a_retry_backoff_max_seconds,
                            self.settings.a2a_retry_backoff_base_seconds * (2**attempt),
                        ) * random.uniform(0.5, 1.0)
                        logger.warning(
                            f"A2A call to '{target_agent}' returned {resp.status_code}; retrying in {backoff:.2f}s (attempt {attempt+1}/{max_attempts})."
                        )
                        await asyncio.sleep(backoff)
                        continue

                    if not self.settings.allow_local_agent_fallback:
                        raise A2ACommunicationError(
                            target_agent=target_agent,
                            message=f"A2A endpoint rejected the request with HTTP {resp.status_code}.",
                        )
                    logger.warning("A2A HTTP error; using development-only local fallback.")
                    break
                except A2ACommunicationError:
                    raise
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    if attempt < max_attempts - 1:
                        backoff = min(
                            self.settings.a2a_retry_backoff_max_seconds,
                            self.settings.a2a_retry_backoff_base_seconds * (2**attempt),
                        ) * random.uniform(0.5, 1.0)
                        logger.warning(
                            f"A2A transport error connecting to '{target_agent}': {exc}; retrying in {backoff:.2f}s (attempt {attempt+1}/{max_attempts})."
                        )
                        await asyncio.sleep(backoff)
                        continue
                    if not self.settings.allow_local_agent_fallback:
                        raise A2ACommunicationError(
                            target_agent=target_agent,
                            message="A2A transport is unavailable.",
                        ) from exc
                    logger.info("A2A transport unavailable; using development fallback.")
                    break

        # Local fallback execution
        local_graph = self._get_local_graph(target_agent)
        if not local_graph:
            A2A_REQUESTS_TOTAL.labels(
                caller="supervisor", target_agent=target_agent, status="error"
            ).inc()
            raise A2ACommunicationError(
                target_agent=target_agent,
                message=f"Sub-agent '{target_agent}' is not registered and local fallback failed.",
            )

        card = get_agent_card(target_agent)
        if not card:
            A2A_REQUESTS_TOTAL.labels(
                caller="supervisor", target_agent=target_agent, status="error"
            ).inc()
            raise A2ACommunicationError(
                target_agent=target_agent,
                message=f"AgentCard for '{target_agent}' not found.",
            )
        from src.agentic.a2a.executor import A2AAgentExecutor

        executor = A2AAgentExecutor(card=card, graph=local_graph)
        res = await executor.execute(req)
        status = "success" if res.status == "completed" else "error"
        A2A_REQUESTS_TOTAL.labels(
            caller="supervisor", target_agent=target_agent, status=status
        ).inc()
        return res

    async def call_agent_stream(
        self,
        target_agent: str,
        step_id: str,
        instruction: str,
        context: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        timeout: float = 60.0,
    ) -> AsyncIterator[dict[str, Any]]:
        """Streams sub-agent tokens and lifecycle events over A2A SSE with local fallback."""
        base_url = self._endpoints.get(target_agent)
        endpoint = f"{base_url}/tasks/stream" if base_url else None

        req = A2ATaskRequest(
            task_id=step_id,
            caller="supervisor",
            target_agent=target_agent,
            payload=A2ATaskPayload(instruction=instruction, context=context or {}),
            metadata=metadata or {},
        )

        headers: dict[str, str] = {
            "X-Internal-Service-Token": self.settings.s2s_internal_token,
        }
        current_span = trace.get_current_span()
        if current_span.is_recording():
            TraceContextTextMapPropagator().inject(headers)

        if endpoint:
            max_attempts = max(1, self.settings.a2a_max_retries)
            for attempt in range(max_attempts):
                try:
                    async with self._concurrency_semaphore:
                        client = self._get_http_client(timeout=timeout)
                        async with client.stream(
                            "POST", endpoint, json=req.model_dump(), headers=headers, timeout=timeout
                        ) as resp:
                            if resp.status_code == 200:
                                A2A_REQUESTS_TOTAL.labels(
                                    caller="supervisor", target_agent=target_agent, status="success"
                                ).inc()
                                async for line in resp.aiter_lines():
                                    if line.startswith("data:"):
                                        data_str = line[5:].strip()
                                        if data_str:
                                            try:
                                                yield json.loads(data_str)
                                            except Exception:
                                                pass
                                return
                            if (
                                resp.status_code in {429, 502, 503, 504}
                                and attempt < max_attempts - 1
                            ):
                                backoff = min(
                                    self.settings.a2a_retry_backoff_max_seconds,
                                    self.settings.a2a_retry_backoff_base_seconds * (2**attempt),
                                ) * random.uniform(0.5, 1.0)
                                logger.warning(
                                    f"A2A streaming to '{target_agent}' returned {resp.status_code}; retrying in {backoff:.2f}s (attempt {attempt+1}/{max_attempts})."
                                )
                                await asyncio.sleep(backoff)
                                continue

                            if not self.settings.allow_local_agent_fallback:
                                raise A2ACommunicationError(
                                    target_agent=target_agent,
                                    message=f"A2A streaming endpoint returned HTTP {resp.status_code}.",
                                )
                            logger.warning(
                                "A2A stream HTTP error; using development-only local fallback."
                            )
                            break
                except A2ACommunicationError:
                    raise
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    if attempt < max_attempts - 1:
                        backoff = min(
                            self.settings.a2a_retry_backoff_max_seconds,
                            self.settings.a2a_retry_backoff_base_seconds * (2**attempt),
                        ) * random.uniform(0.5, 1.0)
                        logger.warning(
                            f"A2A streaming transport error for '{target_agent}': {exc}; retrying in {backoff:.2f}s (attempt {attempt+1}/{max_attempts})."
                        )
                        await asyncio.sleep(backoff)
                        continue
                    if not self.settings.allow_local_agent_fallback:
                        raise A2ACommunicationError(
                            target_agent=target_agent,
                            message="A2A streaming transport is unavailable.",
                        ) from exc
                    logger.info("A2A streaming transport unavailable; using development fallback.")
                    break

        local_graph = self._get_local_graph(target_agent)
        card = get_agent_card(target_agent)
        if not local_graph or not card:
            raise A2ACommunicationError(
                target_agent=target_agent,
                message=f"Sub-agent '{target_agent}' local streaming fallback unavailable.",
            )

        from src.agentic.a2a.executor import A2AAgentExecutor

        executor = A2AAgentExecutor(card=card, graph=local_graph)
        async for ev in executor.execute_stream(req):
            yield ev


@lru_cache
def get_a2a_client() -> A2AClient:
    """Singleton getter for A2AClient."""
    return A2AClient()
