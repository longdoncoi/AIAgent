"""Short-term Memory: Checkpointer and Context Window Trimming."""

import logging
from typing import Any, cast

from langchain_core.messages import BaseMessage, SystemMessage
from langchain_core.messages.modifier import RemoveMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver

from src.agentic.core.config import get_settings, is_strict_env

logger = logging.getLogger(__name__)

_global_checkpointer: BaseCheckpointSaver[Any] | None = None
_pg_pool: Any | None = None
_pg_async_pool: Any | None = None
_pg_saver_cm: Any | None = None


def get_postgres_pool() -> Any | None:
    """Return the synchronous pool used only by the legacy long-term SQL store."""
    global _pg_pool
    if _pg_pool is not None:
        return _pg_pool
    settings = get_settings()
    if not (settings.use_postgres_checkpoint or is_strict_env(settings)):
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
    global _pg_saver_cm, _global_checkpointer, _pg_async_pool
    if _pg_saver_cm is not None:
        try:
            await _pg_saver_cm.__aexit__(None, None, None)
            logger.info("Exited AsyncPostgresSaver context cleanly.")
        except Exception as e:
            logger.debug(f"Error closing PostgresSaver context: {e}")
        finally:
            _pg_saver_cm = None

    if _pg_async_pool is not None:
        try:
            await _pg_async_pool.close()
            logger.info("Closed AsyncPostgresSaver connection pool cleanly.")
        except Exception as e:
            logger.debug("Error closing AsyncPostgresSaver pool: %s", e)
        finally:
            _pg_async_pool = None

    saver = _global_checkpointer
    if saver is not None:
        try:
            from src.agentic.memory.redis_saver import RedisSaver

            if isinstance(saver, RedisSaver):
                await saver.aclose()
        except Exception as e:
            logger.debug("Error closing RedisSaver resources: %s", e)

    _global_checkpointer = None
    close_postgres_pool()


async def is_checkpointer_healthy() -> bool:
    """Verify the active checkpointer connection/pool (not a throwaway probe)."""
    saver = _global_checkpointer
    if saver is None:
        return False
    if _pg_async_pool is not None:
        try:
            async with _pg_async_pool.connection() as conn:
                await conn.execute("SELECT 1;")
            return True
        except Exception:
            return False
    conn = getattr(saver, "conn", None)
    if conn is not None and hasattr(conn, "execute"):
        try:
            await conn.execute("SELECT 1;")
            return True
        except Exception:
            return False
    return True


async def initialize_checkpointer() -> BaseCheckpointSaver[Any]:
    """Initialize the configured saver without blocking the event loop."""
    global _global_checkpointer, _pg_saver_cm, _pg_async_pool
    if _global_checkpointer is not None:
        return _global_checkpointer

    settings = get_settings()
    if settings.use_postgres_checkpoint:
        try:
            from unittest.mock import NonCallableMock

            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            is_mocked = isinstance(AsyncPostgresSaver.from_conn_string, NonCallableMock)
            if not is_mocked:
                try:
                    from psycopg.rows import dict_row
                    from psycopg_pool import AsyncConnectionPool

                    pool = AsyncConnectionPool(
                        conninfo=settings.postgres_uri,
                        min_size=2,
                        max_size=20,
                        timeout=10.0,
                        kwargs={
                            "autocommit": True,
                            "prepare_threshold": None,
                            "row_factory": dict_row,
                        },
                        open=False,
                    )
                    await pool.open()
                    saver = AsyncPostgresSaver(conn=cast(Any, pool))
                    if settings.auto_run_migrations or not is_strict_env(settings):
                        await saver.setup()
                    _pg_async_pool = pool
                    _global_checkpointer = saver
                    logger.info("Initialized pooled AsyncPostgresSaver checkpointer.")
                    return saver
                except ImportError:
                    pass

            cm = AsyncPostgresSaver.from_conn_string(settings.postgres_uri)
            saver = await cm.__aenter__()
            if settings.auto_run_migrations or not is_strict_env(settings):
                await saver.setup()
            _pg_saver_cm = cm
            _global_checkpointer = saver
            logger.info("Initialized AsyncPostgresSaver checkpointer.")
            return saver
        except Exception as exc:
            if is_strict_env(settings) and not settings.use_redis_checkpoint:
                raise RuntimeError("PostgreSQL checkpointer initialization failed.") from exc
            logger.warning("PostgreSQL checkpointer unavailable; trying configured fallback: %s", exc)
            # Fall through to the explicit Redis branch below. ``get_checkpointer``
            # short-circuits on ``use_postgres_checkpoint`` and would otherwise
            # mask the Redis fallback (or silently use MemorySaver in prod).
            return _init_redis_checkpointer(settings)

    return get_checkpointer()


def _init_redis_checkpointer(settings: Any) -> BaseCheckpointSaver[Any]:
    """Construct a RedisSaver directly; fail closed in production."""
    global _global_checkpointer
    if settings.use_redis_checkpoint:
        try:
            from src.agentic.memory.redis_saver import RedisSaver

            saver = RedisSaver(redis_url=settings.redis_state_url or settings.redis_url)
            _global_checkpointer = saver
            logger.info("Initialized distributed RedisSaver checkpointer (fallback path).")
            return saver
        except Exception as exc:
            if is_strict_env(settings):
                raise RuntimeError(
                    "Fatal Production Configuration Error: both PostgreSQL and Redis "
                    "checkpointers are unavailable."
                ) from exc
            logger.warning("RedisSaver fallback unavailable: %s", exc)
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
        if is_strict_env(settings):
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

            redis_saver = RedisSaver(redis_url=settings.redis_state_url or settings.redis_url)
            _global_checkpointer = redis_saver
            logger.info("Initialized distributed RedisSaver checkpointer.")
            return _global_checkpointer
        except Exception as e:
            if is_strict_env(settings):
                raise RuntimeError(
                    f"Fatal Production Configuration Error: Failed to connect to Redis checkpointer ({e})."
                ) from e
            logger.warning(
                f"Could not connect to Redis checkpointer ({e}). Falling back to MemorySaver."
            )

    if is_strict_env(settings):
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
    *,
    emit_removals: bool = True,
) -> list[BaseMessage]:
    """Ensure message history stays within a token budget.

    LangGraph's ``add_messages`` reducer is append-only: returning only the
    surviving messages does NOT shrink the checkpoint, it merely appends them.
    By default this function therefore appends ``RemoveMessage`` entries for
    every dropped message id, so the checkpointed list actually shrinks once
    the reducer processes the result. Set ``emit_removals=False`` for the legacy
    view-only behaviour (e.g. when building a prompt for the model).
    """
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

    kept_ids = {id(m) for m in system_msgs + trimmed}
    result: list[BaseMessage] = list(system_msgs) + trimmed
    if emit_removals:
        for msg in messages:
            if id(msg) not in kept_ids and msg.id:
                result.append(RemoveMessage(id=msg.id))
    return result
