"""Model Gateway resolving model aliases to LiteLLM, vLLM, or llama.cpp."""

import logging
import threading
import time
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import LLMResult
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from src.agentic.cache.kv_cache import get_kv_cache_headers
from src.agentic.core.config import get_settings
from src.agentic.observability.metrics import TOKENS_TOTAL

logger = logging.getLogger(__name__)


class TokenMetricsCallbackHandler(AsyncCallbackHandler):
    """Tracks prompt and completion tokens consumed per model alias into Prometheus metrics."""

    def __init__(self, model_alias: str) -> None:
        super().__init__()
        self.model_alias = model_alias

    async def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        _circuit_breaker.record_success(self.model_alias)
        if response.llm_output and "token_usage" in response.llm_output:
            usage = response.llm_output["token_usage"]
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            if prompt_tokens:
                TOKENS_TOTAL.labels(model_alias=self.model_alias, token_type="prompt").inc(
                    prompt_tokens
                )
            if completion_tokens:
                TOKENS_TOTAL.labels(model_alias=self.model_alias, token_type="completion").inc(
                    completion_tokens
                )

    async def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        """Trip the shared breaker from real provider failures."""
        _circuit_breaker.record_failure(self.model_alias)
        logger.warning("Model request failed for alias '%s': %s", self.model_alias, type(error).__name__)


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


def reload_gateway_config() -> None:
    """Dynamically resets circuit breaker and model gateway state on config reload."""
    global _circuit_breaker
    with _circuit_breaker._lock:
        _circuit_breaker.failures.clear()
        _circuit_breaker.last_failure_time.clear()
        _circuit_breaker.state.clear()
        _circuit_breaker.trip_count.clear()
    logger.info("ModelCircuitBreaker state reset during gateway config reload.")


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

    # Check circuit breaker status
    if _circuit_breaker.should_fallback(alias):
        logger.warning(
            f"Circuit breaker active for alias '{alias}'. Falling back to CPU / fast-llm endpoint."
        )
        alias = "fast-llm"
        model_name = model_name_map.get(alias, alias)
        base_url = settings.llama_chat_base_url

    is_test = settings.environment == "test"

    # Fast check if gateway endpoint is reachable
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    target_host = parsed.hostname or "localhost"
    target_port = parsed.port or 4000

    def _is_active() -> bool:
        try:
            with socket.create_connection((target_host, target_port), timeout=0.2):
                return True
        except Exception:
            return False

    is_online = _is_active()
    effective_timeout = (
        timeout if timeout is not None else (0.5 if (is_test or not is_online) else 60.0)
    )
    max_retries = 0 if (is_test or not is_online) else 2

    # Include KV-cache and token tracking callbacks
    callbacks = kwargs.pop("callbacks", []) or []
    callbacks.append(TokenMetricsCallbackHandler(model_alias=alias))

    default_headers = get_kv_cache_headers() if settings.enable_kv_cache_prefix_optimization else {}
    if "extra_headers" in kwargs:
        default_headers.update(kwargs.pop("extra_headers"))

    logger.debug(
        f"Resolved LLM alias '{alias}' -> model='{model_name}' at base_url='{base_url}' (online={is_online}, timeout={effective_timeout}s)"
    )

    return ChatOpenAI(
        model=model_name,
        base_url=base_url,
        api_key=SecretStr(api_key),
        temperature=temperature,
        streaming=streaming,
        timeout=effective_timeout,
        max_retries=max_retries,
        callbacks=callbacks,
        default_headers=default_headers,
        **kwargs,
    )
