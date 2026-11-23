"""Short-term Memory: Checkpointer and Context Window Trimming."""

import logging
from typing import Any

from langchain_core.messages import BaseMessage, SystemMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver

from src.agentic.core.config import get_settings

logger = logging.getLogger(__name__)

_global_checkpointer: BaseCheckpointSaver[Any] | None = None
_pg_pool: Any | None = None
_pg_saver_cm: Any | None = None


def get_postgres_pool() -> Any | None:
    """Return the synchronous pool used only by the legacy long-term SQL store."""
    global _pg_pool
    if _pg_pool is not None:
        return _pg_pool
    settings = get_settings()
    if not (settings.use_postgres_checkpoint or settings.environment == "production"):
        return None
    try:
        from psycopg_pool import ConnectionPool

        _pg_pool = ConnectionPool(
            conninfo=settings.postgres_uri,
            min_size=2,
            max_size=20,
            timeout=10.0,
            kwargs={"prepare_threshold": None},
            open=False,
        )
        _pg_pool.open()
    except Exception as exc:
        logger.warning("Could not initialize PostgreSQL long-term pool: %s", exc)
        _pg_pool = None
    return _pg_pool


def close_postgres_pool() -> None:
    """Compatibility shutdown hook for the synchronous long-term-memory pool."""
    global _pg_pool, _global_checkpointer, _pg_saver_cm
    if _pg_saver_cm is not None and hasattr(_pg_saver_cm, "__exit__"):
        try:
            _pg_saver_cm.__exit__(None, None, None)
        except Exception as exc:
            logger.debug("Error closing legacy saver context: %s", exc)
        _pg_saver_cm = None
    if _pg_pool is not None:
        try:
            _pg_pool.close()
        except Exception as exc:
            logger.warning("Error closing PostgreSQL pool: %s", exc)
        _pg_pool = None
    _global_checkpointer = None


async def close_checkpointer() -> None:
    """Gracefully close the asynchronous checkpointer context."""
    global _pg_saver_cm, _global_checkpointer
    if _pg_saver_cm is not None:
        try:
            await _pg_saver_cm.__aexit__(None, None, None)
            logger.info("Exited AsyncPostgresSaver context cleanly.")
        except Exception as e:
            logger.debug(f"Error closing PostgresSaver context: {e}")
        finally:
            _pg_saver_cm = None

    _global_checkpointer = None
    close_postgres_pool()


async def initialize_checkpointer() -> BaseCheckpointSaver[Any]:
    """Initialize the configured saver without blocking the event loop."""
    global _global_checkpointer, _pg_saver_cm
    if _global_checkpointer is not None:
        return _global_checkpointer

    settings = get_settings()
    if settings.use_postgres_checkpoint:
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            cm = AsyncPostgresSaver.from_conn_string(settings.postgres_uri)
            saver = await cm.__aenter__()
            await saver.setup()
            _pg_saver_cm = cm
            _global_checkpointer = saver
            logger.info("Initialized AsyncPostgresSaver checkpointer.")
            return saver
        except Exception as exc:
            if settings.environment == "production" and not settings.use_redis_checkpoint:
                raise RuntimeError("PostgreSQL checkpointer initialization failed.") from exc
            logger.warning("PostgreSQL checkpointer unavailable; trying configured fallback: %s", exc)

    return get_checkpointer()


def get_checkpointer() -> BaseCheckpointSaver[Any]:
    """Returns the active checkpointer:
    1. PostgresSaver if use_postgres_checkpoint is enabled and reachable.
    2. MemorySaver supporting async/await graph execution for tests and local nodes.
    """
    global _global_checkpointer, _pg_saver_cm
    if _global_checkpointer is not None:
        return _global_checkpointer

    settings = get_settings()

    # PostgreSQL requires async lifecycle initialization from the application lifespan.
    if settings.use_postgres_checkpoint:
        if settings.environment == "production":
            raise RuntimeError(
                "Fatal Production Configuration Error: PostgreSQL checkpointer is not initialized; "
                "call initialize_checkpointer() during lifespan startup."
            )
        if settings.environment == "test":
            try:
                from langgraph.checkpoint.postgres import PostgresSaver

                cm = PostgresSaver.from_conn_string(settings.postgres_uri)
                saver = cm.__enter__()
                if hasattr(saver, "setup"):
                    saver.setup()
                _pg_saver_cm = cm
                _global_checkpointer = saver
                return saver
            except Exception as exc:
                logger.debug("Test-only sync PostgresSaver compatibility failed: %s", exc)
        logger.warning("PostgreSQL saver is not initialized; using MemorySaver outside production.")
        _global_checkpointer = MemorySaver()
        return _global_checkpointer

    # 2. Try RedisSaver if enabled
    if settings.use_redis_checkpoint:
        try:
            from src.agentic.memory.redis_saver import RedisSaver

            redis_saver = RedisSaver(redis_url=settings.redis_url)
            _global_checkpointer = redis_saver
            logger.info("Initialized distributed RedisSaver checkpointer.")
            return _global_checkpointer
        except Exception as e:
            if settings.environment == "production":
                raise RuntimeError(
                    f"Fatal Production Configuration Error: Failed to connect to Redis checkpointer ({e})."
                ) from e
            logger.warning(
                f"Could not connect to Redis checkpointer ({e}). Falling back to MemorySaver."
            )

    if settings.environment == "production":
        raise RuntimeError(
            "Fatal Production Configuration Error: neither use_postgres_checkpoint nor use_redis_checkpoint is enabled in production. "
            "In-memory MemorySaver is strictly prohibited across multiple replicas."
        )

    # 2. MemorySaver supporting both async (ainvoke) and sync execution natively
    _global_checkpointer = MemorySaver()
    logger.info("Initialized in-memory MemorySaver checkpointer.")
    return _global_checkpointer


def trim_messages_window(
    messages: list[BaseMessage],
    max_tokens: int = 4000,
) -> list[BaseMessage]:
    """Ensures message history remains within token budget, keeping system prompt intact."""
    if not messages:
        return []

    system_msgs = [m for m in messages if isinstance(m, SystemMessage)]
    non_system_msgs = [m for m in messages if not isinstance(m, SystemMessage)]

    # Keep latest messages up to max_tokens roughly (4 chars ~ 1 token)
    char_budget = max_tokens * 4
    current_chars = 0
    trimmed: list[BaseMessage] = []

    for msg in reversed(non_system_msgs):
        content_len = len(str(msg.content))
        if current_chars + content_len > char_budget and trimmed:
            break
        trimmed.insert(0, msg)
        current_chars += content_len

    return system_msgs + trimmed
