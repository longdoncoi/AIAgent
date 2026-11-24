"""FastAPI middlewares for distributed tracing, metrics, request-id, and rate limiting."""

import asyncio
import ipaddress
import logging
import threading
import time
import uuid
from collections import defaultdict
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import StreamingResponse

from src.agentic.core.config import get_settings
from src.agentic.core.context import REQUEST_ID_VAR
from src.agentic.observability.metrics import REQUEST_DURATION
from src.agentic.safety.policy import get_policy_manager

logger = logging.getLogger(__name__)

# REQUEST_ID_VAR is defined in core/context and re-exported here.

_MAX_BODY_BYTES = 2 * 1024 * 1024
_RATE_LIMIT_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, ARGV[1])
local count = redis.call('ZCARD', KEYS[1])
if count >= tonumber(ARGV[2]) then
    local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
    local retry = 60
    if oldest[2] then
        retry = math.ceil(tonumber(oldest[2]) + 60 - tonumber(ARGV[3]))
    end
    if retry < 1 then retry = 1 end
    return -retry
end
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[4])
redis.call('EXPIRE', KEYS[1], 65)
return 1
"""
_TOKEN_LIMIT_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, ARGV[1])
local entries = redis.call('ZRANGE', KEYS[1], 0, -1, 'WITHSCORES')
local total = 0
local oldest_score = nil
for i = 1, #entries, 2 do
    local match = string.match(entries[i], ":(%d+):")
    if match then total = total + tonumber(match) end
    if not oldest_score then oldest_score = tonumber(entries[i + 1]) end
end
if total + tonumber(ARGV[2]) > tonumber(ARGV[3]) then
    local retry = 60
    if oldest_score then
        retry = math.ceil(oldest_score + 60 - tonumber(ARGV[4]))
    end
    if retry < 1 then retry = 1 end
    return -retry
end
redis.call('ZADD', KEYS[1], ARGV[4], ARGV[5])
redis.call('EXPIRE', KEYS[1], 65)
return 1
"""


class RateLimiterUnavailable(RuntimeError):
    """Raised when the authoritative distributed limiter cannot be reached."""


def _parse_content_length(request: Request) -> int:
    """Parse a client-supplied Content-Length without ever raising."""
    raw = request.headers.get("content-length")
    if not raw:
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return value if value >= 0 else 0


def _parse_retry(result: Any) -> tuple[bool, int]:
    """Translate a Lua result into (allowed, retry_after)."""
    if isinstance(result, (int, float)):
        value = int(result)
        return (True, 0) if value == 1 else (False, abs(value) or 1)
    return True, 0


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
        if limit_per_minute < 1:
            raise ValueError("limit_per_minute must be >= 1")
        if token_limit_per_minute < 1:
            raise ValueError("token_limit_per_minute must be >= 1")
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
                logger.info("Initialized Distributed Redis Rate Limiter at %s", redis_url)
            except Exception as e:
                # Redis is the authoritative limiter in production: fail fast
                # instead of silently degrading to per-process memory.
                logger.error("Could not connect to Redis for rate limiting: %s", e)
                raise RateLimiterUnavailable(
                    "Redis rate limiter is enabled but unreachable"
                ) from e

    def _eval(self, script: str, key: str, args: list[Any]) -> tuple[bool, int]:
        """Execute an atomic Lua script. The non-atomic pipeline fallback was removed."""
        if self._redis_client is None:
            raise RateLimiterUnavailable("No Redis client configured")
        try:
            result = self._redis_client.eval(script, 1, key, *args)
        except Exception as ex:
            logger.error("Redis rate limiter evaluation failed: %s", ex)
            if self.enable_redis:
                raise RateLimiterUnavailable("Redis rate limiter unavailable") from ex
            raise
        return _parse_retry(result)

    def is_allowed(self, client_key: str, cost: int = 1) -> tuple[bool, int]:
        """Checks if request count is within limit_per_minute."""
        now = time.time()
        window_start = now - 60.0

        if self._redis_client is not None:
            redis_key = f"rl:{client_key}"
            member = f"{now}:{uuid.uuid4().hex[:6]}"
            return self._eval(
                _RATE_LIMIT_SCRIPT,
                redis_key,
                [window_start, self.limit, now, member],
            )

        with self._lock:
            if len(self.requests) > 500:
                stale_keys = [
                    k for k, v in self.requests.items() if not v or v[-1] <= window_start
                ]
                for k in stale_keys:
                    del self.requests[k]
                while len(self.requests) > 1000:
                    self.requests.pop(next(iter(self.requests)))

            valid_times = [t for t in self.requests[client_key] if t > window_start]
            if len(valid_times) >= self.limit:
                retry_after = max(1, int(60.0 - (now - valid_times[0]))) if valid_times else 60
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
            redis_key = f"rl:tokens:{client_key}"
            member = f"{now}:{tokens}:{uuid.uuid4().hex[:6]}"
            return self._eval(
                _TOKEN_LIMIT_SCRIPT,
                redis_key,
                [window_start, tokens, self.token_limit, now, member],
            )

        with self._lock:
            if len(self.token_requests) > 500:
                stale_keys = [
                    k for k, v in self.token_requests.items() if not v or v[-1][0] <= window_start
                ]
                for k in stale_keys:
                    del self.token_requests[k]
                while len(self.token_requests) > 1000:
                    self.token_requests.pop(next(iter(self.token_requests)))

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


class FailClosedRateLimiter:
    """Sentinel used when the authoritative production limiter is unavailable."""

    def is_allowed(self, client_key: str, cost: int = 1) -> tuple[bool, int]:
        raise RateLimiterUnavailable("Authoritative Redis rate limiter is unavailable")

    def is_token_allowed(self, client_key: str, tokens: int) -> tuple[bool, int]:
        raise RateLimiterUnavailable("Authoritative Redis rate limiter is unavailable")


_settings = get_settings()
_rate_limiter: SlidingWindowRateLimiter | FailClosedRateLimiter
try:
    _rate_limiter = SlidingWindowRateLimiter(
        limit_per_minute=_settings.rate_limit_per_minute,
        token_limit_per_minute=_settings.token_rate_limit_per_minute,
        redis_url=_settings.redis_url,
        enable_redis=_settings.enable_redis_rate_limit,
        enable_token_limit=_settings.enable_token_rate_limit,
    )
except RateLimiterUnavailable:
    # Only reachable if an operator enabled Redis but the service is down.
    # Degrade to a local limiter with a loud warning rather than crashing at
    # import; production readiness checks must catch this configuration.
    if _settings.is_production:
        logger.critical("Redis rate limiter unavailable at startup; failing closed")
        _rate_limiter = FailClosedRateLimiter()
    else:
        logger.error("Redis rate limiter unavailable at startup; using in-memory limiter")
        _rate_limiter = SlidingWindowRateLimiter(
            limit_per_minute=_settings.rate_limit_per_minute,
            token_limit_per_minute=_settings.token_rate_limit_per_minute,
            enable_redis=False,
            enable_token_limit=_settings.enable_token_rate_limit,
        )

_TRUSTED_PROXIES: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(cidr, strict=False)
    for cidr in _settings.trusted_proxies
    if cidr.strip()
)


def _is_trusted_proxy(ip: str) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(address in network for network in _TRUSTED_PROXIES)


def resolve_client_ip(request: Request) -> str:
    """Resolve the client IP, trusting X-Forwarded-For only behind known proxies."""
    peer = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded or not _is_trusted_proxy(peer):
        return peer
    for candidate in reversed([p.strip() for p in forwarded.split(",") if p.strip()]):
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if not _is_trusted_proxy(str(address)):
            return str(address)
    return peer


def estimate_tokens_from_request(
    content_length: int,
    content_type: str = "",
    is_chunked: bool = False,
    actual_bytes: int | None = None,
) -> int:
    """Estimate request tokens from measured payload bytes.

    ``Content-Length`` and ``Transfer-Encoding`` are client-controlled, so the
    middleware passes the actual number of bytes read from the body when
    available. Chunked requests no longer receive a free 50-token allowance.
    """
    size = actual_bytes if actual_bytes is not None else content_length
    if size <= 0:
        # Preserve the legacy floor for callers that only pass a header value;
        # an actually measured empty body legitimately costs zero prompt tokens.
        return 10 if actual_bytes is None else 0
    if actual_bytes is None and is_chunked:
        # Backward-compatible fallback only when we cannot measure the body.
        return max(10, int(size / 3.2))
    effective_bytes = max(1, size - 30) if size > 40 else size
    floor = 10 if actual_bytes is None else 1
    return max(floor, int(effective_bytes / 3.2))


async def _read_body_with_cap(request: Request) -> int:
    """Read the body via ``body()`` so BaseHTTPMiddleware replays it downstream.

    Calling ``stream()`` here would consume the receive channel and hand an
    empty body to the endpoint. ``body()`` is cached by Starlette and replayed
    by ``_CachedRequest.wrapped_receive``.
    """
    declared = _parse_content_length(request)
    if declared > _MAX_BODY_BYTES:
        raise ValueError("Request body exceeds maximum permitted size")
    body = await request.body()
    if len(body) > _MAX_BODY_BYTES:
        raise ValueError("Request body exceeds maximum permitted size")
    return len(body)


class RequestIDAndMetricsMiddleware(BaseHTTPMiddleware):
    """Enforces Rate Limiting, OpenTelemetry Distributed Tracing, and Prometheus latency measurement."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        token = REQUEST_ID_VAR.set(request_id)
        try:
            return await self._dispatch(request, call_next, request_id)
        finally:
            REQUEST_ID_VAR.reset(token)

    async def _dispatch(
        self, request: Request, call_next: RequestResponseEndpoint, request_id: str
    ) -> Response:
        path = request.url.path
        start_time = time.perf_counter()

        # Preflight requests must never consume quota or be rejected.
        if request.method != "OPTIONS" and not path.startswith(("/health", "/ready", "/metrics")):
            client_ip = resolve_client_ip(request)
            rate_key = f"ip:{client_ip}"
            try:
                allowed, retry_after = await asyncio.to_thread(
                    _rate_limiter.is_allowed, rate_key
                )
            except RateLimiterUnavailable:
                return self._error_response(
                    request_id, 503, "Rate limiter unavailable", None
                )
            if not allowed:
                logger.warning("Rate limit exceeded for client: %s", client_ip)
                return self._error_response(
                    request_id,
                    429,
                    "Rate limit exceeded. Please slow down.",
                    {"Retry-After": str(retry_after)},
                )

            if _settings.enable_token_rate_limit and path.startswith(
                ("/api/v1/chat", "/a2a/tasks", "/tasks")
            ):
                try:
                    actual_bytes = await _read_body_with_cap(request)
                except ValueError:
                    return self._error_response(request_id, 413, "Payload too large", None)

                estimated_tokens = estimate_tokens_from_request(
                    _parse_content_length(request),
                    request.headers.get("content-type", ""),
                    is_chunked=request.headers.get("transfer-encoding", "").lower() == "chunked",
                    actual_bytes=actual_bytes,
                )
                # LLM cost includes generated tokens, not only prompt bytes.
                max_output = int(
                    get_policy_manager().get_guardrails_config().get("max_output_tokens", 4096)
                )
                estimated_tokens += max_output
                tok_allowed, tok_retry = await asyncio.to_thread(
                    _rate_limiter.is_token_allowed, rate_key, estimated_tokens
                )
                if not tok_allowed:
                    logger.warning(
                        "Token tier rate limit exceeded for client %s (estimated=%d)",
                        client_ip,
                        estimated_tokens,
                    )
                    return self._error_response(
                        request_id,
                        429,
                        f"Token tier rate limit exceeded ({_settings.token_rate_limit_per_minute} tokens/min).",
                        {"Retry-After": str(tok_retry)},
                    )

        tracer = trace.get_tracer("agentic-api")
        extracted_context = TraceContextTextMapPropagator().extract(carrier=request.headers)
        span_name = f"HTTP {request.method} {request.url.path}"
        with tracer.start_as_current_span(span_name, context=extracted_context) as span:
            span.set_attribute("http.method", request.method)
            span.set_attribute("http.url", str(request.url))
            span.set_attribute("http.request_id", request_id)

            trace_id = format(span.get_span_context().trace_id, "032x")
            status_label = "error"
            streaming = False
            try:
                response = await call_next(request)
                status_label = "success" if response.status_code < 400 else "error"
                streaming = isinstance(response, StreamingResponse)
            except Exception as e:
                logger.error(
                    "Unhandled exception on %s %s: %s", request.method, request.url.path, e,
                    exc_info=True,
                )
                span.record_exception(e)
                span.set_status(trace.StatusCode.ERROR, str(e))
                self._set_correlation_headers(response := self._error_response(
                    request_id, 500, "Internal Server Error", None
                ), request_id, trace_id, time.perf_counter() - start_time)
                return response
            finally:
                duration = time.perf_counter() - start_time
                # Streaming bodies are consumed after dispatch returns; observe
                # their latency when the background body finishes instead.
                if not streaming:
                    REQUEST_DURATION.labels(
                        component="api", status=status_label
                    ).observe(duration)

            self._set_correlation_headers(response, request_id, trace_id, time.perf_counter() - start_time)
            if streaming:
                response.background = self._compose_background(
                    response.background,
                    BackgroundTask(
                        self._observe_stream_duration,
                        start_time,
                        status_label,
                    ),
                )
            return response

    @staticmethod
    def _observe_stream_duration(start_time: float, status_label: str) -> None:
        """Observe streaming latency only after the response body has completed."""
        REQUEST_DURATION.labels(component="api", status=status_label).observe(
            time.perf_counter() - start_time
        )

    @staticmethod
    def _set_correlation_headers(
        response: Response, request_id: str, trace_id: str, duration: float
    ) -> None:
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Trace-ID"] = trace_id
        response.headers["X-Response-Time"] = f"{duration:.4f}s"

    @staticmethod
    def _error_response(
        request_id: str,
        status_code: int,
        detail: str,
        extra_headers: dict[str, str] | None,
    ) -> JSONResponse:
        response = JSONResponse(
            status_code=status_code,
            content={"error": "Request rejected", "detail": detail},
        )
        if extra_headers:
            response.headers.update(extra_headers)
        response.headers["X-Request-ID"] = request_id
        return response

    @staticmethod
    def _compose_background(current: Any, added: BackgroundTask) -> BackgroundTask:
        from starlette.background import BackgroundTasks

        if current is None:
            return added
        tasks = current if isinstance(current, BackgroundTasks) else BackgroundTasks([current])
        tasks.add_task(added.func, *added.args, **added.kwargs)
        return tasks
