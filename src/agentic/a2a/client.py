"""A2A Client used by Supervisor to dispatch tasks to Sub-Agents."""

import asyncio
import hashlib
import json
import logging
import random
import re
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any, ClassVar

import httpx
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from prometheus_client import Counter

from src.agentic.a2a.cards import get_agent_card
from src.agentic.a2a.executor import A2ATaskPayload, A2ATaskRequest, A2ATaskResponse
from src.agentic.a2a.identity import sign_request
from src.agentic.core.config import get_settings
from src.agentic.core.context import get_request_id
from src.agentic.core.errors import A2ACommunicationError
from src.agentic.observability.metrics import A2A_REQUESTS_TOTAL

logger = logging.getLogger(__name__)

# Dedicated metric for malformed SSE frames. A2A_REQUESTS_TOTAL keeps the
# request outcome (success/error) while this counter isolates parser failures.
A2A_SSE_PARSE_ERRORS_TOTAL = Counter(
    "agent_a2a_sse_parse_errors_total",
    "Malformed SSE data frames received from A2A sub-agent streams",
    ["caller", "target_agent"],
)

_LIFECYCLE_EVENT_RE = re.compile(r'"event"\s*:\s*"(completed|error)"', re.IGNORECASE)


def _derive_idempotency_key(
    target_agent: str,
    step_id: str,
    metadata: dict[str, Any] | None,
) -> str:
    """Derives an idempotency key isolated by tenant, owner, thread, turn, and step."""
    meta = metadata or {}
    tenant_id = str(meta.get("tenant_id") or "")
    owner_id = str(meta.get("owner_id") or "")
    thread_id = str(meta.get("thread_id") or "")
    turn_id = str(meta.get("turn_id") or "")
    base_key = str(
        meta.get("idempotency_key")
        or meta.get("step_fingerprint")
        or f"{target_agent}:{step_id}"
    )
    raw = f"{target_agent}:{tenant_id}:{owner_id}:{thread_id}:{turn_id}:{base_key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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

    def _build_headers(self) -> dict[str, str]:
        """Build outbound headers: service token, trace context and request id."""
        headers: dict[str, str] = {
            "X-Internal-Service-Token": self.settings.s2s_internal_token,
        }
        current_span = trace.get_current_span()
        if current_span.is_recording():
            TraceContextTextMapPropagator().inject(headers)

        request_id = get_request_id()
        if request_id and request_id != "-":
            headers["X-Request-ID"] = request_id
        return headers

    def _record_request(self, target_agent: str, status: str) -> None:
        """Increment the A2A request counter exactly once per dispatch."""
        A2A_REQUESTS_TOTAL.labels(
            caller="supervisor", target_agent=target_agent, status=status
        ).inc()

    _LOCAL_GRAPH_LOADERS: ClassVar[dict[str, tuple[str, str]]] = {
        "chat": ("src.agentic.agents.chat.graph", "get_chat_graph"),
        "rag": ("src.agentic.agents.rag.graph", "get_rag_graph"),
        "coding": ("src.agentic.agents.coding.graph", "get_coding_graph"),
    }

    def _get_local_graph(self, agent_name: str) -> Any:
        """Lazy-loads sub-agent graph for in-process fallback (synchronous)."""
        norm_name = agent_name.replace("_agent", "")
        if norm_name not in self._local_executors:
            loader = self._LOCAL_GRAPH_LOADERS.get(norm_name)
            if loader is not None:
                import importlib

                module_path, attr_name = loader
                module = importlib.import_module(module_path)
                factory = getattr(module, attr_name)
                self._local_executors[norm_name] = factory()
        return self._local_executors.get(norm_name)

    async def _get_local_graph_async(self, agent_name: str) -> Any:
        """Offload the import-heavy graph factory so it never blocks the event loop."""
        return await asyncio.to_thread(self._get_local_graph, agent_name)

    def _record_sse_parse_error(self, target_agent: str, payload: str, exc: Exception) -> None:
        """Surface a malformed SSE frame instead of silently swallowing it."""
        A2A_SSE_PARSE_ERRORS_TOTAL.labels(
            caller="supervisor", target_agent=target_agent
        ).inc()
        logger.warning(
            "Dropping malformed A2A SSE data frame from '%s': %s (payload=%.160s)",
            target_agent,
            exc,
            payload,
        )

    @staticmethod
    def _raise_if_broken_lifecycle(payload: str, target_agent: str, step_id: str) -> None:
        """A corrupt completed/error event must never look like an empty success."""
        if _LIFECYCLE_EVENT_RE.search(payload):
            raise A2ACommunicationError(
                target_agent=target_agent,
                message=(
                    f"Malformed A2A lifecycle event for step '{step_id}'; refusing to "
                    "treat an empty stream as completed."
                ),
            )

    def _decode_sse_frames(
        self, buffered: list[str], target_agent: str, step_id: str
    ) -> list[dict[str, Any]]:
        """Decode one SSE event, joining consecutive ``data:`` lines per spec.

        Servers that emit one JSON object per ``data:`` line without a blank
        separator are still supported through a per-line recovery pass.
        """
        joined = "\n".join(buffered)
        try:
            obj = json.loads(joined)
        except json.JSONDecodeError as exc:
            if len(buffered) == 1:
                self._record_sse_parse_error(target_agent, joined, exc)
                self._raise_if_broken_lifecycle(joined, target_agent, step_id)
                return []
            decoded: list[dict[str, Any]] = []
            for line in buffered:
                try:
                    line_obj = json.loads(line)
                except json.JSONDecodeError as line_exc:
                    self._record_sse_parse_error(target_agent, line, line_exc)
                    self._raise_if_broken_lifecycle(line, target_agent, step_id)
                    continue
                if isinstance(line_obj, dict):
                    decoded.append(line_obj)
                else:
                    self._record_sse_parse_error(
                        target_agent, line, ValueError("SSE payload is not a JSON object")
                    )
            return decoded

        if isinstance(obj, dict):
            return [obj]
        self._record_sse_parse_error(
            target_agent, joined, ValueError("SSE payload is not a JSON object")
        )
        return []

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
        status = "error"
        try:
            base_url = self._endpoints.get(target_agent)
            endpoint = f"{base_url}/tasks" if base_url else None

            idem_key = _derive_idempotency_key(target_agent, step_id, metadata)
            req = A2ATaskRequest(
                task_id=step_id,
                caller="supervisor",
                target_agent=target_agent,
                payload=A2ATaskPayload(instruction=instruction, context=context or {}),
                metadata=metadata or {},
                idempotency_key=idem_key,
            )
            sign_request(req, self.settings)

            headers = self._build_headers()
            headers["X-Idempotency-Key"] = idem_key

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
                            response = A2ATaskResponse(**resp.json())
                            status = "success"
                            return response
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

            # Local fallback execution (forbidden unless explicitly allowed)
            if not self.settings.allow_local_agent_fallback:
                raise A2ACommunicationError(
                    target_agent=target_agent,
                    message=f"A2A endpoint unavailable and local fallback is disabled for '{target_agent}'.",
                )
            local_graph = await self._get_local_graph_async(target_agent)
            if not local_graph:
                raise A2ACommunicationError(
                    target_agent=target_agent,
                    message=f"Sub-agent '{target_agent}' is not registered and local fallback failed.",
                )

            card = get_agent_card(target_agent)
            if not card:
                raise A2ACommunicationError(
                    target_agent=target_agent,
                    message=f"AgentCard for '{target_agent}' not found.",
                )
            from src.agentic.a2a.executor import A2AAgentExecutor

            executor = A2AAgentExecutor(card=card, graph=local_graph)
            res = await executor.execute(req)
            status = "success" if res.status == "completed" else "error"
            return res
        finally:
            self._record_request(target_agent, status)

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
        status = "error"
        try:
            base_url = self._endpoints.get(target_agent)
            endpoint = f"{base_url}/tasks/stream" if base_url else None

            idem_key = _derive_idempotency_key(target_agent, step_id, metadata)
            req = A2ATaskRequest(
                task_id=step_id,
                caller="supervisor",
                target_agent=target_agent,
                payload=A2ATaskPayload(instruction=instruction, context=context or {}),
                metadata=metadata or {},
                idempotency_key=idem_key,
            )
            sign_request(req, self.settings)

            headers = self._build_headers()
            headers["X-Idempotency-Key"] = idem_key

            if endpoint:
                max_attempts = max(1, self.settings.a2a_max_retries)
                for attempt in range(max_attempts):
                    stream_started = False
                    try:
                        async with self._concurrency_semaphore:
                            client = self._get_http_client(timeout=timeout)
                            async with client.stream(
                                "POST", endpoint, json=req.model_dump(), headers=headers, timeout=timeout
                            ) as resp:
                                if resp.status_code == 200:
                                    buffered: list[str] = []
                                    lifecycle_seen = False
                                    async for line in resp.aiter_lines():
                                        if line == "":
                                            if buffered:
                                                frames = buffered
                                                buffered = []
                                                for frame in self._decode_sse_frames(
                                                    frames, target_agent, step_id
                                                ):
                                                    stream_started = True
                                                    status = "success"
                                                    if frame.get("event") in {"completed", "error"}:
                                                        lifecycle_seen = True
                                                    yield frame
                                            continue
                                        if line.startswith("data:"):
                                            # SSE removes exactly one leading space.
                                            buffered.append(line[5:].lstrip(" "))
                                        # `event:`, `id:`, `retry:` and comments are metadata.
                                    if buffered:
                                        for frame in self._decode_sse_frames(
                                            buffered, target_agent, step_id
                                        ):
                                            stream_started = True
                                            status = "success"
                                            if frame.get("event") in {"completed", "error"}:
                                                lifecycle_seen = True
                                            yield frame
                                    if not lifecycle_seen:
                                        raise A2ACommunicationError(
                                            target_agent=target_agent,
                                            message=(
                                                f"A2A stream for step '{step_id}' ended with HTTP 200 "
                                                "but no completed/error lifecycle event; refusing "
                                                "to treat a dead sub-agent as success."
                                            ),
                                        )
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
                        if stream_started:
                            raise A2ACommunicationError(
                                target_agent=target_agent,
                                message=(
                                    f"A2A stream for step '{step_id}' failed mid-stream after "
                                    "first frame; refusing non-idempotent retry."
                                ),
                            ) from exc
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

            if not self.settings.allow_local_agent_fallback:
                raise A2ACommunicationError(
                    target_agent=target_agent,
                    message=f"A2A streaming endpoint unavailable and local fallback is disabled for '{target_agent}'.",
                )
            local_graph = await self._get_local_graph_async(target_agent)
            card = get_agent_card(target_agent)
            if not local_graph or not card:
                raise A2ACommunicationError(
                    target_agent=target_agent,
                    message=f"Sub-agent '{target_agent}' local streaming fallback unavailable.",
                )

            from src.agentic.a2a.executor import A2AAgentExecutor

            executor = A2AAgentExecutor(card=card, graph=local_graph)
            async for ev in executor.execute_stream(req):
                if isinstance(ev, dict) and ev.get("event") in {"token", "completed"}:
                    status = "success"
                yield ev
        finally:
            self._record_request(target_agent, status)


@lru_cache
def get_a2a_client() -> A2AClient:
    """Singleton getter for A2AClient."""
    return A2AClient()
