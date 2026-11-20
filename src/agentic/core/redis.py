"""Centralized Singleton Redis Connection Pools for High-Throughput Production Multi-Tenancy."""

import logging
import threading
from typing import Any

from src.agentic.core.config import get_settings

logger = logging.getLogger(__name__)

_sync_pool: Any | None = None
_async_pool: Any | None = None
_pool_lock = threading.Lock()


def get_redis_connection_pool(redis_url: str | None = None, max_connections: int = 100) -> Any:
    """Returns singleton synchronous redis.ConnectionPool instance."""
    global _sync_pool
    if _sync_pool is not None:
        return _sync_pool

    with _pool_lock:
        if _sync_pool is None:
            settings = get_settings()
            target_url = redis_url or settings.redis_url
            try:
                import redis

                _sync_pool = redis.ConnectionPool.from_url(
                    target_url,
                    max_connections=max_connections,
                    socket_timeout=0.5,
                    socket_connect_timeout=1.0,
                    decode_responses=True,
                    health_check_interval=30,
                )
                logger.info(
                    f"Initialized singleton sync Redis ConnectionPool (max_connections={max_connections}) at {target_url}"
                )
            except Exception as e:
                logger.debug(f"Could not initialize sync Redis ConnectionPool ({e})")
                _sync_pool = None
    return _sync_pool


def get_async_redis_connection_pool(
    redis_url: str | None = None, max_connections: int = 100
) -> Any:
    """Returns singleton asynchronous redis.asyncio.ConnectionPool instance."""
    global _async_pool
    if _async_pool is not None:
        return _async_pool

    with _pool_lock:
        if _async_pool is None:
            settings = get_settings()
            target_url = redis_url or settings.redis_url
            try:
                import redis.asyncio as aioredis

                _async_pool = aioredis.ConnectionPool.from_url(
                    target_url,
                    max_connections=max_connections,
                    socket_timeout=2.0,
                    socket_connect_timeout=2.0,
                    decode_responses=True,
                    health_check_interval=30,
                )
                logger.info(
                    f"Initialized singleton async Redis ConnectionPool (max_connections={max_connections}) at {target_url}"
                )
            except Exception as e:
                logger.debug(f"Could not initialize async Redis ConnectionPool ({e})")
                _async_pool = None
    return _async_pool


def get_redis_client(redis_url: str | None = None) -> Any | None:
    """Returns a redis.Redis client backed by the singleton connection pool."""
    pool = get_redis_connection_pool(redis_url=redis_url)
    if pool is not None:
        try:
            import redis

            return redis.Redis(connection_pool=pool)
        except Exception as e:
            logger.debug(f"Error creating Redis client from pool: {e}")
    return None


def get_async_redis_client(redis_url: str | None = None) -> Any | None:
    """Returns an async redis.asyncio.Redis client backed by the singleton async connection pool."""
    pool = get_async_redis_connection_pool(redis_url=redis_url)
    if pool is not None:
        try:
            import redis.asyncio as aioredis

            return aioredis.Redis(connection_pool=pool)
        except Exception as e:
            logger.debug(f"Error creating async Redis client from pool: {e}")
    return None


def close_redis_pools() -> None:
    """Gracefully closes all Redis connection pools on application shutdown."""
    global _sync_pool, _async_pool
    with _pool_lock:
        if _sync_pool is not None:
            try:
                _sync_pool.disconnect()
                logger.info("Closed synchronous Redis ConnectionPool cleanly.")
            except Exception as e:
                logger.warning(f"Error disconnecting sync Redis pool: {e}")
            finally:
                _sync_pool = None

        if _async_pool is not None:
            try:
                import asyncio

                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(_async_pool.disconnect())
                except RuntimeError:
                    # No running event loop
                    pass
                logger.info("Triggered async Redis ConnectionPool disconnect.")
            except Exception as e:
                logger.warning(f"Error disconnecting async Redis pool: {e}")
            finally:
                _async_pool = None
