"""Model Gateway resolving model aliases to LiteLLM (all traffic stays on the router).

The gateway deliberately does NOT inspect, reorder, or strip messages: system
prompts (including the planner's) must survive regardless of whether
``enable_kv_cache_prefix_optimization`` is on or off. KV-cache handling is
limited to HTTP headers set by :func:`get_kv_cache_headers`.

Callers that use LangGraph ``astream_events`` must request ``streaming=True``
here (the gateway does not guess streaming intent).
"""

import asyncio
import logging
import threading
import time
from typing import Any

import yaml
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import LLMResult
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr, SecretStr

from src.agentic.cache.kv_cache import get_kv_cache_headers
from src.agentic.cache.prompt_cache import get_prompt_cache
from src.agentic.core.config import Settings, get_settings
from src.agentic.memory.data_lifecycle import cost_tags_from_context
from src.agentic.observability.metrics import (
    COST_ALLOCATION_TOTAL,
    TOKEN_COST_TOTAL,
    TOKENS_TOTAL,
)
from src.agentic.tools.execution_context import get_tool_execution_context

logger = logging.getLogger(__name__)

# Fallback alias configured in configs/litellm_config.yaml. It must be routed
# through LiteLLM (same base_url/api_key as every other alias) -- bypassing the
# router with a direct llama.cpp URL was the original credential/alias bug.
_FALLBACK_ALIAS = "llama-chat-backup"

# Indicative, self-hosted-equivalent USD price per 1K tokens: (prompt, completion).
# Hardcoded on purpose so cost is always observable; these are NOT billing-grade
# tariffs and should be replaced by real provider pricing when metering exists.
_PRICE_PER_1K_USD: dict[str, tuple[float, float]] = {
    "reasoning-llm": (0.012, 0.024),  # ~72B reasoning class
    "fast-llm": (0.002, 0.004),  # ~14B chat class
    "coding-llm": (0.006, 0.012),  # ~32B coder class
    "llama-chat-backup": (0.002, 0.004),  # CPU/llama.cpp backup
    "other": (0.005, 0.010),  # bounded default for unknown custom aliases
}


def _bounded_alias(alias: str) -> str:
    """Keep metric label cardinality bounded: map unknown aliases to 'other'."""
    return alias if alias in _PRICE_PER_1K_USD else "other"


def _record_cost(alias: str, prompt_tokens: int, completion_tokens: int) -> None:
    """Record estimated cost after a call/stream using the alias price map."""
    bounded = _bounded_alias(alias)
    prompt_price, completion_price = _PRICE_PER_1K_USD[bounded]
    if prompt_tokens:
        TOKEN_COST_TOTAL.labels(model_alias=bounded, token_type="prompt").inc(
            prompt_tokens / 1000.0 * prompt_price
        )
    if completion_tokens:
        TOKEN_COST_TOTAL.labels(model_alias=bounded, token_type="completion").inc(
            completion_tokens / 1000.0 * completion_price
        )
    tags = cost_tags_from_context()
    total_cost = (
        prompt_tokens / 1000.0 * prompt_price + completion_tokens / 1000.0 * completion_price
    )
    COST_ALLOCATION_TOTAL.labels(
        client_id=tags["client_id"], department=tags["department"], env=tags["env"]
    ).inc(total_cost)


def _validate_fallback_alias(settings: Settings, failed_alias: str) -> str:
    """Return a valid LiteLLM-router fallback alias, or raise with a clear error.

    A fallback is only usable when it is defined in the LiteLLM config with an
    api_base and api_key. Otherwise we refuse to fall back rather than silently
    talk to a direct endpoint with the wrong credentials/alias.
    """
    config_path = settings.litellm_config_path
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(
            f"Circuit breaker is OPEN for '{failed_alias}' but the LiteLLM config "
            f"at {config_path} could not be read ({exc}); refusing to fall back."
        ) from exc

    entries = config.get("model_list", []) if isinstance(config, dict) else []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("model_name") != _FALLBACK_ALIAS:
            continue
        params = entry.get("litellm_params")
        if (
            isinstance(params, dict)
            and params.get("api_base")
            and params.get("api_key")
        ):
            return _FALLBACK_ALIAS
    raise RuntimeError(
        f"Circuit breaker is OPEN for '{failed_alias}' but no valid fallback alias "
        f"'{_FALLBACK_ALIAS}' is configured in {config_path}; refusing to bypass "
        "the LiteLLM router with mismatched credentials."
    )


class _AwaitableNone:
    """Helper enabling callback methods to be called synchronously or awaited."""

    def __await__(self) -> Any:
        async def _noop() -> None:
            pass

        return _noop().__await__()


class _PromptCachedChatOpenAI(ChatOpenAI):
    """ChatOpenAI with exact-match prompt caching on invoke/ainvoke.

    Cache namespace is alias + temperature + SHA-256 of the FULL message list,
    plus the request owner/tenant id when available, so tenant-specific prompts
    never collide and raw prompt text is never persisted (only the hash key and
    the response text). Streaming calls and tool/structured-output calls bypass
    the cache entirely, so the streaming path pays no cache overhead.
    """

    _cache_alias: str = PrivateAttr(default="")
    _cache_enabled: bool = PrivateAttr(default=True)

    def _cache_messages(self, input: Any) -> list[Any] | None:
        """Build the message list used for the cache-key hash (never sent to the model)."""
        if isinstance(input, str):
            messages: list[Any] = [input]
        elif isinstance(input, (list, tuple)):
            messages = list(input)
        elif hasattr(input, "to_messages"):
            messages = list(input.to_messages())
        else:
            return None
        context = get_tool_execution_context()
        if context is not None and context.owner_id:
            messages.append({"role": "system", "content": f"tenant_scope:{context.owner_id}"})
        return messages

    def _should_use_cache(self, kwargs: dict[str, Any]) -> bool:
        if not self._cache_enabled or self.streaming or kwargs:
            return False
        if self.temperature is not None and self.temperature > 0.7:
            return False
        return True

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        if not self._should_use_cache(kwargs):
            return super().invoke(input, config=config, **kwargs)
        messages = self._cache_messages(input)
        if messages is None:
            return super().invoke(input, config=config, **kwargs)

        cache = get_prompt_cache()
        temperature = float(self.temperature or 0.0)
        cached = cache.get_response(self._cache_alias, messages, temperature)
        if cached is not None:
            return AIMessage(content=cached)
        response = super().invoke(input, config=config, **kwargs)
        if isinstance(response, AIMessage) and isinstance(response.content, str):
            cache.set_response(self._cache_alias, messages, temperature, response.content)
        return response

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        if not self._should_use_cache(kwargs):
            return await super().ainvoke(input, config=config, **kwargs)
        messages = self._cache_messages(input)
        if messages is None:
            return await super().ainvoke(input, config=config, **kwargs)

        cache = get_prompt_cache()
        temperature = float(self.temperature or 0.0)
        cached = await asyncio.to_thread(
            cache.get_response, self._cache_alias, messages, temperature
        )
        if cached is not None:
            return AIMessage(content=cached)
        response = await super().ainvoke(input, config=config, **kwargs)
        if isinstance(response, AIMessage) and isinstance(response.content, str):
            await asyncio.to_thread(
                cache.set_response, self._cache_alias, messages, temperature, response.content
            )
        return response


class TokenMetricsCallbackHandler(AsyncCallbackHandler):
    """Tracks tokens and estimated cost per model alias into Prometheus metrics.

    Supports sync invoke, async ainvoke and streamed calls. Providers that do
    not return token usage (common for streaming) are estimated from character
    counts (~4 chars/token) so cost is still observable.
    """

    def __init__(self, model_alias: str) -> None:
        super().__init__()
        self.model_alias = model_alias
        self._bounded_alias = _bounded_alias(model_alias)
        # Per-run counters: model instances are shared across concurrent requests.
        self._prompt_chars_by_run: dict[str, int] = {}
        self._completion_chars_by_run: dict[str, int] = {}
        self._lock = threading.Lock()
        self._prompt_chars = 0
        self._completion_chars = 0

    def _consume_chars(self, run_id: str | None) -> tuple[int, int]:
        key = str(run_id or "default")
        with self._lock:
            prompt = self._prompt_chars_by_run.pop(key, 0)
            completion = self._completion_chars_by_run.pop(key, 0)
        return prompt, completion

    def _record_success(self, response: LLMResult, run_id: str | None = None) -> None:
        _circuit_breaker.record_success(self.model_alias)
        usage: dict[str, Any] = {}
        if response.llm_output and "token_usage" in response.llm_output:
            raw_usage = response.llm_output["token_usage"]
            if isinstance(raw_usage, dict):
                usage = raw_usage
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        run_prompt, run_completion = self._consume_chars(run_id)
        if not prompt_tokens:
            prompt_tokens = run_prompt // 4
        if not completion_tokens:
            completion_tokens = run_completion // 4

        if prompt_tokens:
            TOKENS_TOTAL.labels(model_alias=self._bounded_alias, token_type="prompt").inc(
                prompt_tokens
            )
        if completion_tokens:
            TOKENS_TOTAL.labels(model_alias=self._bounded_alias, token_type="completion").inc(
                completion_tokens
            )
        _record_cost(self.model_alias, prompt_tokens, completion_tokens)

    def _record_error(self, error: BaseException) -> None:
        """Trip the shared breaker from real provider failures (5xx/connection), ignoring 4xx client errors."""
        status = getattr(error, "status_code", None) or getattr(error, "http_status", None)
        client_error = status in {400, 401, 403, 404, 422}
        class_name = type(error).__name__.lower()
        if not client_error and any(
            m in class_name for m in ("badrequest", "notfound", "permission", "unauthorized", "validation")
        ):
            client_error = True
        if client_error:
            logger.debug("Client error on alias '%s'; not tripping breaker: %s", self.model_alias, type(error).__name__)
            return
        _circuit_breaker.record_failure(self.model_alias)
        logger.warning(
            "Model request failed for alias '%s': %s", self.model_alias, type(error).__name__
        )

    def on_llm_start(self, serialized: dict[str, Any], prompts: list[str], **kwargs: Any) -> Any:
        key = str(kwargs.get("run_id") or "default")
        with self._lock:
            self._prompt_chars_by_run[key] = sum(len(prompt) for prompt in prompts)
            self._completion_chars_by_run[key] = 0
        return _AwaitableNone()

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        **kwargs: Any,
    ) -> Any:
        key = str(kwargs.get("run_id") or "default")
        with self._lock:
            self._prompt_chars_by_run[key] = sum(
                len(str(message.content)) for batch in messages for message in batch
            )
            self._completion_chars_by_run[key] = 0
        return _AwaitableNone()

    def on_llm_new_token(
        self,
        token: str | list[str | dict[str, Any]],
        *,
        chunk: Any = None,
        run_id: Any = None,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        # Signature matches AsyncCallbackHandler/LLMManagerMixin; token may be
        # a structured list for some providers.
        key = str(run_id or "default")
        with self._lock:
            self._completion_chars_by_run[key] = (
                self._completion_chars_by_run.get(key, 0)
                + (len(token) if isinstance(token, str) else len(str(token)))
            )
        return _AwaitableNone()

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> Any:
        self._record_success(response, kwargs.get("run_id"))
        return _AwaitableNone()

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> Any:
        self._record_error(error)
        return _AwaitableNone()


class ModelCircuitBreaker:
    """Circuit Breaker tracking model endpoint health and failing over to CPU / backup endpoints."""

    def __init__(self, failure_threshold: int = 3, recovery_time_seconds: float = 30.0) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_time_seconds = recovery_time_seconds
        self.failures: dict[str, int] = {}
        self.last_failure_time: dict[str, float] = {}
        self.state: dict[str, str] = {}  # CLOSED, OPEN, HALF_OPEN
        self.trip_count: dict[str, int] = {}
        self._lock = threading.Lock()

    def record_success(self, alias: str) -> None:
        with self._lock:
            self.failures[alias] = 0
            self.trip_count[alias] = 0
            self.state[alias] = "CLOSED"

    def record_failure(self, alias: str) -> None:
        with self._lock:
            self.failures[alias] = self.failures.get(alias, 0) + 1
            self.last_failure_time[alias] = time.time()
            if self.failures[alias] >= self.failure_threshold:
                self.state[alias] = "OPEN"
                self.trip_count[alias] = self.trip_count.get(alias, 0) + 1
                logger.warning(
                    f"Circuit Breaker tripped to OPEN for model alias '{alias}' "
                    f"(consecutive trips: {self.trip_count[alias]})"
                )

    def should_fallback(self, alias: str) -> bool:
        with self._lock:
            st = self.state.get(alias, "CLOSED")
            if st == "CLOSED":
                return False
            if st == "OPEN":
                now = time.time()
                trips = min(self.trip_count.get(alias, 1), 5)
                effective_recovery = self.recovery_time_seconds * (1.2 ** (trips - 1))
                if now - self.last_failure_time.get(alias, 0) > effective_recovery:
                    self.state[alias] = "HALF_OPEN"
                    return False
                return True
            return False


_circuit_breaker = ModelCircuitBreaker()
_socket_cache: dict[tuple[str, int], tuple[float, bool]] = {}
_socket_cache_lock = threading.Lock()
SOCKET_PROBE_TTL = 5.0  # seconds


def check_endpoint_reachability(
    host: str,
    port: int,
    timeout: float = 0.2,
    ttl: float = SOCKET_PROBE_TTL,
) -> bool:
    """Checks TCP socket reachability with TTL caching to avoid blocking repeated calls."""
    now = time.time()
    key = (host, port)
    if ttl > 0.0:
        with _socket_cache_lock:
            if key in _socket_cache:
                ts, status = _socket_cache[key]
                if now - ts < ttl:
                    return status

    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            is_active = True
    except Exception:
        is_active = False

    if ttl > 0.0:
        with _socket_cache_lock:
            _socket_cache[key] = (now, is_active)
    return is_active


def reload_gateway_config() -> None:
    """Dynamically resets circuit breaker and model gateway state on config reload."""
    global _circuit_breaker
    with _circuit_breaker._lock:
        _circuit_breaker.failures.clear()
        _circuit_breaker.last_failure_time.clear()
        _circuit_breaker.state.clear()
        _circuit_breaker.trip_count.clear()
    with _socket_cache_lock:
        _socket_cache.clear()
    logger.info("ModelCircuitBreaker and gateway socket cache reset during config reload.")


def get_llm(
    alias: str = "reasoning-llm",
    temperature: float = 0.2,
    streaming: bool = False,
    timeout: float | None = None,
    **kwargs: Any,
) -> BaseChatModel:
    """Instantiates a ChatOpenAI client pointing to the configured LiteLLM Gateway.

    Aliases:
    - 'reasoning-llm': High-capacity model for Supervisor, Planning, Reflection, RAG.
    - 'fast-llm': Fast, low-latency model for ChatAgent.
    - 'coding-llm': Code generation & debugging model for CodingAgent.

    All traffic (including circuit-breaker fallback) goes through LiteLLM. The
    gateway never removes or rewrites SystemMessages, so planner system prompts
    survive with KV-cache prefix optimization enabled or disabled. For LangGraph
    ``astream_events`` streaming, callers must pass ``streaming=True`` here.
    """
    settings = get_settings()

    # Map aliases to model identifiers configured in LiteLLM router
    model_name_map = {
        "reasoning-llm": settings.default_reasoning_model,
        "fast-llm": settings.default_fast_model,
        "coding-llm": settings.default_coding_model,
    }

    model_name = model_name_map.get(alias, alias)
    base_url = settings.litellm_base_url
    api_key = settings.litellm_api_key

    # Check circuit breaker status. Fallback is a *router alias*, not a direct
    # llama.cpp URL: reusing litellm_api_key with a raw endpoint (or reusing the
    # 'fast-llm' alias whose credentials differ) was the original credential bug.
    if _circuit_breaker.should_fallback(alias):
        fallback_alias = _validate_fallback_alias(settings, alias)
        logger.warning(
            "Circuit breaker active for alias '%s'. Routing to LiteLLM fallback "
            "alias '%s' via %s (no direct endpoint bypass).",
            alias,
            fallback_alias,
            base_url,
        )
        alias = fallback_alias
        model_name = fallback_alias

    # Only the validated Settings label may select test-mode timeouts.
    is_test_or_ci = settings.environment == "test"

    # Enforce deterministic, configured timeouts without blocking sync socket probes
    effective_timeout = (
        timeout if timeout is not None else (0.5 if is_test_or_ci else 60.0)
    )
    max_retries = 0 if is_test_or_ci else 2
    # Hard generation ceiling from settings; never let a caller silently drop it.
    effective_max_tokens = int(kwargs.pop("max_tokens", settings.max_output_tokens))

    # Include KV-cache and token tracking callbacks
    callbacks = kwargs.pop("callbacks", []) or []
    callbacks.append(TokenMetricsCallbackHandler(model_alias=alias))

    default_headers = get_kv_cache_headers() if settings.enable_kv_cache_prefix_optimization else {}
    if "extra_headers" in kwargs:
        default_headers.update(kwargs.pop("extra_headers"))

    logger.debug(
        f"Resolved LLM alias '{alias}' -> model='{model_name}' at base_url='{base_url}' (timeout={effective_timeout}s)"
    )

    # ChatOpenAI aliases max_tokens -> max_completion_tokens in newer
    # langchain-openai releases, so build the kwargs dynamically to keep the
    # configured ceiling explicit without tripping strict mypy call-arg checks.
    chat_model_kwargs: dict[str, Any] = {
        "model": model_name,
        "base_url": base_url,
        "api_key": SecretStr(api_key),
        "temperature": temperature,
        "streaming": streaming,
        "timeout": effective_timeout,
        "max_tokens": effective_max_tokens,
        "max_retries": max_retries,
        "callbacks": callbacks,
        "default_headers": default_headers,
    }
    llm = _PromptCachedChatOpenAI(**chat_model_kwargs, **kwargs)
    llm._cache_alias = _bounded_alias(alias)
    llm._cache_enabled = settings.enable_prompt_cache
    return llm
