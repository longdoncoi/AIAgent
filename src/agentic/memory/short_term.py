"""Short-term Memory: Checkpointer and Context Window Trimming."""

import logging
from typing import TYPE_CHECKING, Any

from langchain_core.messages import BaseMessage, SystemMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool
else:
    ConnectionPool = Any

from src.agentic.core.config import get_settings

logger = logging.getLogger(__name__)

_global_checkpointer: BaseCheckpointSaver | None = None
_pg_pool: Any | None = None
_pg_saver_cm: Any | None = None


def get_postgres_pool() -> Any | None:
    """Returns singleton PostgreSQL ConnectionPool for high-throughput concurrency."""
    global _pg_pool
    if _pg_pool is None:
        settings = get_settings()
        if settings.use_postgres_checkpoint or settings.environment == "production":
            try:
                from psycopg_pool import ConnectionPool as _RealConnectionPool
                _pg_pool = _RealConnectionPool(
                    conninfo=settings.postgres_uri,
                    min_size=2,
                    max_size=20,
                    timeout=10.0,
                    kwargs={"prepare_threshold": None},
                    open=False,
                )
                _pg_pool.open()
                logger.info("Initialized psycopg ConnectionPool (min_size=2, max_size=20, prepare_threshold=None).")
            except Exception as e:
                logger.warning(f"Could not initialize ConnectionPool ({e}).")
    return _pg_pool


def close_postgres_pool() -> None:
    """Gracefully closes PostgreSQL ConnectionPool and checkpointer context on process shutdown."""
    global _pg_pool, _pg_saver_cm, _global_checkpointer
    if _pg_saver_cm is not None:
        try:
            _pg_saver_cm.__exit__(None, None, None)
            logger.info("Exited PostgresSaver context cleanly.")
        except Exception as e:
            logger.debug(f"Error closing PostgresSaver context: {e}")
        finally:
            _pg_saver_cm = None

    if _pg_pool is not None:
        try:
            _pg_pool.close()
            logger.info("Closed PostgreSQL ConnectionPool gracefully.")
        except Exception as e:
            logger.warning(f"Error while closing ConnectionPool: {e}")
        finally:
            _pg_pool = None

    _global_checkpointer = None


def get_checkpointer() -> BaseCheckpointSaver:
    """Returns the active checkpointer:
    1. PostgresSaver if use_postgres_checkpoint is enabled and reachable.
    2. MemorySaver supporting async/await graph execution for tests and local nodes.
    """
    global _global_checkpointer
    if _global_checkpointer is not None:
        return _global_checkpointer

    settings = get_settings()

    # 1. Try PostgreSQL if enabled
    if settings.use_postgres_checkpoint:
        try:
            get_postgres_pool()
            from langgraph.checkpoint.postgres import PostgresSaver  # type: ignore
            # PostgresSaver.from_conn_string is a context manager, enter it to obtain active saver
            cm = PostgresSaver.from_conn_string(settings.postgres_uri)
            saver: BaseCheckpointSaver = cm.__enter__()
            _pg_saver_cm = cm
            if hasattr(saver, "setup"):
                saver.setup()
            _global_checkpointer = saver
            logger.info("Initialized PostgresSaver checkpointer with connection pool support.")
            return _global_checkpointer
        except Exception as e:
            if settings.environment == "production" and not settings.use_redis_checkpoint:
                raise RuntimeError(
                    f"Fatal Production Configuration Error: Failed to connect to Postgres checkpointer ({e}). "
                    "In-memory fallback is strictly prohibited in production to prevent state loss across replicas."
                ) from e
            logger.warning(f"Could not connect to Postgres checkpointer ({e}). Falling back.")

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
            logger.warning(f"Could not connect to Redis checkpointer ({e}). Falling back to MemorySaver.")

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
