"""FastAPI main entrypoint with lifespan management and router mounting."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.agentic.api.middleware import REQUEST_ID_VAR, RequestIDAndMetricsMiddleware
from src.agentic.api.routes.admin import router as admin_router
from src.agentic.api.routes.approvals import router as approvals_router
from src.agentic.api.routes.chat import router as chat_router
from src.agentic.api.routes.health import router as health_router
from src.agentic.core.config import get_settings
from src.agentic.orchestrator.graph import get_supervisor_graph


class RequestContextFilter(logging.Filter):
    """Attach the active request correlation id to every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = REQUEST_ID_VAR.get()
        return True


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s [request_id=%(request_id)s]: %(message)s",
)
for _handler in logging.getLogger().handlers:
    _handler.addFilter(RequestContextFilter())
logger = logging.getLogger("agentic-platform")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manages application lifecycle: initializes graph and background resources."""
    settings = get_settings()
    logger.info(f"Starting Agentic Platform on environment='{settings.environment}'...")

    from src.agentic.core.redis import aclose_redis_pools
    from src.agentic.memory.short_term import initialize_checkpointer

    stop_poller = asyncio.Event()
    poller = asyncio.create_task(_poll_rag_invalidation(stop_poller))
    try:
        checkpointer = await initialize_checkpointer()
        app.state.graph = get_supervisor_graph(checkpointer=checkpointer)
        logger.info("Supervisor LangGraph initialized and cached in app.state.")
        yield
    finally:
        stop_poller.set()
        poller.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await poller
        logger.info("Shutting down Agentic Platform...")
        for label, closer in (
            ("background tasks", _close_background_tasks),
            ("A2A HTTP client", _close_a2a_client),
            ("checkpointer", _close_checkpointer),
            ("thread registry", _close_thread_registry),
            ("Redis pools", aclose_redis_pools),
        ):
            try:
                await closer()
            except Exception as e:
                logger.warning("Error while closing %s: %s", label, e)


async def _poll_rag_invalidation(stop: asyncio.Event) -> None:
    """Apply cross-replica RAG cache invalidations from the shared generation key."""
    from src.agentic.cache.rag_cache import get_rag_cache

    cache = get_rag_cache()
    while not stop.is_set():
        try:
            await asyncio.to_thread(cache.check_invalidation_generation)
        except Exception as e:  # observability must never crash the poller
            logger.warning("RAG invalidation poll failed: %s", e)
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=5.0)


async def _close_background_tasks() -> None:
    from src.agentic.orchestrator.nodes.finalize import wait_all_background_tasks

    await wait_all_background_tasks(timeout=5.0)


async def _close_a2a_client() -> None:
    from src.agentic.a2a.client import get_a2a_client

    await get_a2a_client().aclose()


async def _close_checkpointer() -> None:
    from src.agentic.memory.short_term import close_checkpointer

    await close_checkpointer()


async def _close_thread_registry() -> None:
    from src.agentic.memory.thread_registry import get_thread_registry

    await get_thread_registry().aclose()


def create_app() -> FastAPI:
    """Application factory for FastAPI server."""
    settings = get_settings()
    app = FastAPI(
        title="Agentic Multi-Agent Platform",
        description="Hierarchical Multi-Agent Platform: LangGraph Supervisor, A2A Sub-Agents, FastMCP Tools, LlamaIndex RAG",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS configuration with zero-trust domain policy
    cors_origins = settings.allowed_origins
    if settings.environment in ["development", "test"] and not cors_origins:
        cors_origins = ["*"]

    # Middleware order matters: ``add_middleware`` prepends, so the last added
    # is outermost. CORS must be outermost so early rejections (429/503/413)
    # still carry Access-Control-* headers and preflight is never rate-limited.
    app.add_middleware(RequestIDAndMetricsMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Mount routers
    app.include_router(health_router)
    app.include_router(chat_router)
    app.include_router(approvals_router)
    app.include_router(admin_router)

    return app


app = create_app()
