"""FastAPI main entrypoint with lifespan management and router mounting."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.agentic.api.middleware import RequestIDAndMetricsMiddleware
from src.agentic.api.routes.approvals import router as approvals_router
from src.agentic.api.routes.chat import router as chat_router
from src.agentic.api.routes.health import router as health_router
from src.agentic.core.config import get_settings
from src.agentic.orchestrator.graph import get_supervisor_graph

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("agentic-platform")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manages application lifecycle: initializes graph and background resources."""
    settings = get_settings()
    logger.info(f"Starting Agentic Platform on environment='{settings.environment}'...")

    # Pre-compile the supervisor graph
    app.state.graph = get_supervisor_graph()
    logger.info("Supervisor LangGraph initialized and cached in app.state.")

    yield

    logger.info("Shutting down Agentic Platform...")
    try:
        from src.agentic.orchestrator.nodes.finalize import wait_all_background_tasks
        await wait_all_background_tasks(timeout=5.0)
    except Exception as e:
        logger.warning(f"Error while waiting for background tasks: {e}")

    try:
        from src.agentic.a2a.client import get_a2a_client
        await get_a2a_client().aclose()
    except Exception as e:
        logger.warning(f"Error while closing A2A HTTP client: {e}")

    try:
        from src.agentic.memory.short_term import close_postgres_pool
        close_postgres_pool()
    except Exception as e:
        logger.warning(f"Error while closing PostgreSQL pool: {e}")

    try:
        from src.agentic.core.redis import close_redis_pools
        close_redis_pools()
    except Exception as e:
        logger.warning(f"Error while closing Redis pools: {e}")


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

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Custom tracing and metrics middleware
    app.add_middleware(RequestIDAndMetricsMiddleware)

    # Mount routers
    app.include_router(health_router)
    app.include_router(chat_router)
    app.include_router(approvals_router)

    return app


app = create_app()
