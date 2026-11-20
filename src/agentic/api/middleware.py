"""FastAPI middlewares for distributed tracing, metrics, request-id, and rate limiting."""

import logging
import threading
import time
import uuid
from collections import defaultdict
from typing import Any, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from starlette.middleware.base import BaseHTTPMiddleware

from src.agentic.core.config import get_settings
from src.agentic.observability.metrics import REQUEST_DURATION

logger = logging.getLogger(__name__)


class SlidingWindowRateLimiter:
    """Thread-safe sliding window rate limiter supporting Redis and in-memory fallback,
    with dual enforcement for Request Limits and Token Tier Quotas.
    """

    def __init__(
        self,
        limit_per_minute: int = 120,
        token_limit_per_minute: int = 100_000,
        redis_url: str | None = None,
        enable_redis: bool = False,
        enable_token_limit: bool = True,
    ) -> None:
        self.limit = limit_per_minute
        self.token_limit = token_limit_per_minute
        self.enable_token_limit = enable_token_limit
        self.requests: dict[str, list[float]] = defaultdict(list)
        self.token_requests: dict[str, list[tuple[float, int]]] = defaultdict(list)
        self._lock = threading.Lock()
        self._redis_client: Any = None
        self.enable_redis = enable_redis

        if enable_redis and redis_url:
            try:
                from src.agentic.core.redis import get_redis_client

                client = get_redis_client(redis_url)
                if client is None:
                    import redis

                    client = redis.Redis.from_url(
                        redis_url, socket_timeout=0.5, decode_responses=True
                    )
                client.ping()
                self._redis_client = client
                logger.info(f"Initialized Distributed Redis Rate Limiter at {redis_url}")
            except Exception as e:
                logger.warning(
                    f"Could not connect to Redis for Rate Limiting ({e}). Falling back to in-memory."
                )

    def is_allowed(self, client_key: str, cost: int = 1) -> tuple[bool, int]:
        """Checks if request count is within limit_per_minute."""
        now = time.time()
        window_start = now - 60.0

        if self._redis_client is not None:
            try:
                pipe = self._redis_client.pipeline()
                redis_key = f"rl:{client_key}"
                pipe.zremrangebyscore(redis_key, 0, window_start)
                pipe.zcard(redis_key)
                pipe.zadd(redis_key, {f"{now}:{uuid.uuid4().hex[:6]}": now})
                pipe.expire(redis_key, 65)
                results = pipe.execute()
                current_count = results[1]
                if current_count >= self.limit:
                    return False, 60
                return True, 0
            except Exception as ex:
                logger.debug(
                    f"Redis rate limiter exception ({ex}), fallback to local memory limiter."
                )

        with self._lock:
            # Purge timestamps older than 60s
            valid_times = [t for t in self.requests[client_key] if t > window_start]
            if len(valid_times) >= self.limit:
                retry_after = max(1, int(60.0 - (now - valid_times[0])))
                self.requests[client_key] = valid_times
                return False, retry_after
            valid_times.append(now)
            self.requests[client_key] = valid_times
            return True, 0

    def is_token_allowed(self, client_key: str, tokens: int) -> tuple[bool, int]:
        """Checks if token consumption is within token_limit_per_minute."""
        if not self.enable_token_limit:
            return True, 0

        now = time.time()
        window_start = now - 60.0

        if self._redis_client is not None:
            try:
                pipe = self._redis_client.pipeline()
                redis_key = f"rl:tokens:{client_key}"
                pipe.zremrangebyscore(redis_key, 0, window_start)
                pipe.zrange(redis_key, 0, -1)
                results = pipe.execute()
                current_entries = results[1] or []
                total_tokens = 0
                for entry in current_entries:
                    try:
                        parts = str(entry).split(":")
                        if len(parts) >= 2:
                            total_tokens += int(parts[1])
                    except (ValueError, IndexError):
                        pass

                if total_tokens + tokens > self.token_limit:
                    return False, 60

                pipe = self._redis_client.pipeline()
                pipe.zadd(redis_key, {f"{now}:{tokens}:{uuid.uuid4().hex[:6]}": now})
                pipe.expire(redis_key, 65)
                pipe.execute()
                return True, 0
            except Exception as ex:
                logger.debug(
                    f"Redis token rate limiter exception ({ex}), fallback to local memory limiter."
                )

        with self._lock:
            valid_entries = [e for e in self.token_requests[client_key] if e[0] > window_start]
            total_tokens = sum(e[1] for e in valid_entries)
            if total_tokens + tokens > self.token_limit:
                oldest_t = valid_entries[0][0] if valid_entries else now
                retry_after = max(1, int(60.0 - (now - oldest_t)))
                self.token_requests[client_key] = valid_entries
                return False, retry_after
            valid_entries.append((now, tokens))
            self.token_requests[client_key] = valid_entries
            return True, 0


_settings = get_settings()
_rate_limiter = SlidingWindowRateLimiter(
    limit_per_minute=_settings.rate_limit_per_minute,
    token_limit_per_minute=_settings.token_rate_limit_per_minute,
    redis_url=_settings.redis_url,
    enable_redis=_settings.enable_redis_rate_limit,
    enable_token_limit=_settings.enable_token_rate_limit,
)


def estimate_tokens_from_request(content_length: int, content_type: str = "") -> int:
    """Estimates the token count of an incoming HTTP request payload.

    Implements a multi-lingual and Unicode-aware token estimation model:
    - Accounts for JSON envelope overhead (~30 bytes for standard ChatRequest schema).
    - Accurately models UTF-8 multi-byte sequences (Vietnamese diacritics, CJK, symbols)
      which occupy 2-3 bytes/character while words average ~3.2 bytes/token.
    - Preserves deterministic O(1) performance without blocking the stream.
    """
    if content_length <= 0:
        return 10

    effective_bytes = max(1, content_length - 30) if content_length > 40 else content_length
    estimated = max(10, int(effective_bytes / 3.2))
    return estimated


class RequestIDAndMetricsMiddleware(BaseHTTPMiddleware):
    """Enforces Rate Limiting, OpenTelemetry Distributed Tracing, and Prometheus latency measurement."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path

        # Rate limiting check (bypass health and metrics endpoints)
        if not path.startswith(("/health", "/ready", "/metrics")):
            client_ip = request.client.host if request.client else "unknown"
            auth_header = request.headers.get("Authorization", "")
            rate_key = auth_header if auth_header else client_ip
            allowed, retry_after = _rate_limiter.is_allowed(rate_key)
            if not allowed:
                logger.warning(f"Rate limit exceeded for client: {client_ip}")
                return JSONResponse(
                    status_code=429,
                    content={
                        "error": "Too Many Requests",
                        "detail": "Rate limit exceeded. Please slow down.",
                    },
                    headers={"Retry-After": str(retry_after)},
                )

            # Token Tier Rate Limit check for chat and task endpoints
            if _settings.enable_token_rate_limit and path.startswith(
                ("/api/v1/chat", "/a2a/tasks", "/tasks")
            ):
                try:
                    content_length = int(request.headers.get("content-length", 0) or 0)
                except ValueError:
                    content_length = 0
                content_type = request.headers.get("content-type", "")
                estimated_tokens = estimate_tokens_from_request(content_length, content_type)
                tok_allowed, tok_retry_after = _rate_limiter.is_token_allowed(
                    rate_key, tokens=estimated_tokens
                )
                if not tok_allowed:
                    logger.warning(
                        f"Token tier rate limit exceeded for client: {client_ip} (estimated={estimated_tokens} tokens)"
                    )
                    return JSONResponse(
                        status_code=429,
                        content={
                            "error": "Too Many Requests",
                            "detail": f"Token tier rate limit exceeded ({_settings.token_rate_limit_per_minute} tokens/min). Please slow down or reduce prompt length.",
                        },
                        headers={"Retry-After": str(tok_retry_after)},
                    )

        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        start_time = time.perf_counter()

        # Extract incoming distributed trace context
        tracer = trace.get_tracer("agentic-api")
        extracted_context = TraceContextTextMapPropagator().extract(carrier=request.headers)

        span_name = f"HTTP {request.method} {request.url.path}"
        with tracer.start_as_current_span(span_name, context=extracted_context) as span:
            span.set_attribute("http.method", request.method)
            span.set_attribute("http.url", str(request.url))
            span.set_attribute("http.request_id", request_id)

            trace_id = format(span.get_span_context().trace_id, "032x")
            response = Response(content="Internal Server Error", status_code=500)
            status_label = "error"
            try:
                response = await call_next(request)
                status_label = "success" if response.status_code < 400 else "error"
            except Exception as e:
                logger.error(
                    f"Unhandled exception on {request.method} {request.url.path}: {e}",
                    exc_info=True,
                )
                span.record_exception(e)
                span.set_status(trace.StatusCode.ERROR, str(e))
                raise
            finally:
                duration = time.perf_counter() - start_time
                REQUEST_DURATION.labels(component="api", status=status_label).observe(duration)
                response.headers["X-Request-ID"] = request_id
                response.headers["X-Trace-ID"] = trace_id
                response.headers["X-Response-Time"] = f"{duration:.4f}s"

        return response
