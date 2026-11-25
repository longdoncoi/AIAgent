"""Centralized Singleton Redis Connection Pools for High-Throughput Production Multi-Tenancy."""

import logging
import threading
from typing import Any

from src.agentic.core.config import get_settings

logger = logging.getLogger(__name__)

_sync_pool: Any | None = None
_async_pool: Any | None = None
# Binary pools are mandatory for pickle/HMAC checkpoint payloads: redis-py
# ignores ``decode_responses`` on the client when a connection_pool is passed,
# so the flag must be set on the pool itself.
_binary_sync_pool: Any | None = None
_binary_async_pool: Any | None = None
_pool_lock = threading.Lock()


def get_redis_connection_pool(
    redis_url: str | None = None,
    max_connections: int = 100,
    *,
    decode_responses: bool = True,
) -> Any:
    """Returns a singleton synchronous redis.ConnectionPool.

    ``decode_responses=False`` selects a dedicated binary-safe pool. This is
    required for raw pickle/HMAC payloads (LangGraph checkpoints); otherwise
    redis-py attempts UTF-8 decoding of arbitrary bytes and corrupts them.
    """
    global _sync_pool, _binary_sync_pool
    current = _sync_pool if decode_responses else _binary_sync_pool
    if current is not None:
        return current

    with _pool_lock:
        current = _sync_pool if decode_responses else _binary_sync_pool
        if current is None:
            settings = get_settings()
            target_url = redis_url or settings.redis_url
            try:
                import redis

                current = redis.ConnectionPool.from_url(
                    target_url,
                    max_connections=max_connections,
                    socket_timeout=0.5,
                    socket_connect_timeout=1.0,
                    decode_responses=decode_responses,
                    health_check_interval=30,
                )
                logger.info(
                    "Initialized singleton sync Redis ConnectionPool "
                    "(max_connections=%s, decode_responses=%s) at %s",
                    max_connections,
                    decode_responses,
                    target_url,
                )
            except Exception as e:
                logger.debug(f"Could not initialize sync Redis ConnectionPool ({e})")
                current = None
            if decode_responses:
                _sync_pool = current
            else:
                _binary_sync_pool = current
    return current


def get_async_redis_connection_pool(
    redis_url: str | None = None,
    max_connections: int = 100,
    *,
    decode_responses: bool = True,
) -> Any:
    """Returns a singleton asynchronous redis.asyncio.ConnectionPool.

    As with the sync pool, ``decode_responses=False`` selects a dedicated
    binary-safe pool so checkpoint blobs are never UTF-8 decoded.
    """
    global _async_pool, _binary_async_pool
    current = _async_pool if decode_responses else _binary_async_pool
    if current is not None:
        return current

    with _pool_lock:
        current = _async_pool if decode_responses else _binary_async_pool
        if current is None:
            settings = get_settings()
            target_url = redis_url or settings.redis_url
            try:
                import redis.asyncio as aioredis

                current = aioredis.ConnectionPool.from_url(
                    target_url,
                    max_connections=max_connections,
                    socket_timeout=2.0,
                    socket_connect_timeout=2.0,
                    decode_responses=decode_responses,
                    health_check_interval=30,
                )
                logger.info(
                    "Initialized singleton async Redis ConnectionPool "
                    "(max_connections=%s, decode_responses=%s) at %s",
                    max_connections,
                    decode_responses,
                    target_url,
                )
            except Exception as e:
                logger.debug(f"Could not initialize async Redis ConnectionPool ({e})")
                current = None
            if decode_responses:
                _async_pool = current
            else:
                _binary_async_pool = current
    return current


def get_redis_client(redis_url: str | None = None, decode_responses: bool = True) -> Any | None:
    """Returns a redis.Redis client backed by the singleton connection pool.

    ``decode_responses=False`` is required for binary checkpoint payloads;
    the flag is forwarded to the pool (redis-py ignores it on the client when
    an external pool is supplied).
    """
    pool = get_redis_connection_pool(redis_url=redis_url, decode_responses=decode_responses)
    if pool is not None:
        try:
            import redis

            # Keep the legacy call shape for the default (`True`) so existing
            # instrumentation/mocks keep working; only binary mode needs the
            # explicit client-level override.
            if decode_responses:
                return redis.Redis(connection_pool=pool)
            return redis.Redis(connection_pool=pool, decode_responses=False)
        except Exception as e:
            logger.debug(f"Error creating Redis client from pool: {e}")
    return None


def get_async_redis_client(
    redis_url: str | None = None, *, decode_responses: bool = True
) -> Any | None:
    """Returns an async redis.asyncio.Redis client backed by the singleton async pool."""
    pool = get_async_redis_connection_pool(
        redis_url=redis_url, decode_responses=decode_responses
    )
    if pool is not None:
        try:
            import redis.asyncio as aioredis

            if decode_responses:
                return aioredis.Redis(connection_pool=pool)
            return aioredis.Redis(connection_pool=pool, decode_responses=False)
        except Exception as e:
            logger.debug(f"Error creating async Redis client from pool: {e}")
    return None


def close_redis_pools() -> None:
    """Gracefully closes all synchronous Redis connection pools on shutdown."""
    global _sync_pool, _binary_sync_pool
    with _pool_lock:
        for name, pool in (("sync", _sync_pool), ("binary sync", _binary_sync_pool)):
            if pool is not None:
                try:
                    pool.disconnect()
                    logger.info("Closed %s Redis ConnectionPool cleanly.", name)
                except Exception as e:
                    logger.warning(f"Error disconnecting {name} Redis pool: {e}")
        _sync_pool = None
        _binary_sync_pool = None

    # The async pools are closed by ``aclose_redis_pools`` from the app
    # lifespan so the disconnect is always awaited.


async def aclose_redis_pools() -> None:
    """Await graceful shutdown of the async pools, then close the sync pools."""
    global _async_pool, _binary_async_pool
    for name, pool in (("async", _async_pool), ("binary async", _binary_async_pool)):
        if pool is not None:
            try:
                await pool.disconnect()
                logger.info("Closed %s Redis ConnectionPool cleanly.", name)
            except Exception as e:
                logger.warning("Error disconnecting %s Redis pool: %s", name, e)
    _async_pool = None
    _binary_async_pool = None
    close_redis_pools()


def iter_redis_keys(client: Any, pattern: str, count: int = 200) -> list[Any]:
    """Enumerate matching Redis keys without blocking via SCAN when supported.

    Falls back to ``keys(pattern)`` only for test doubles or legacy clients whose
    type/instance does not explicitly provide ``scan_iter``.
    """
    scan_iter = getattr(client, "scan_iter", None)
    if (
        scan_iter is not None
        and callable(scan_iter)
        and (
            callable(getattr(type(client), "scan_iter", None))
            or "scan_iter" in getattr(client, "__dict__", {})
        )
    ):
        return list(scan_iter(match=pattern, count=count))
    keys = client.keys(pattern)
    return list(keys) if keys else []
