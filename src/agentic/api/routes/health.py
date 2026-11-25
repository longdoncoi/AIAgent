"""Health, deep readiness, and Prometheus metrics endpoints."""

import asyncio
import socket
from urllib.parse import urlparse

from fastapi import APIRouter, Response, status
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from src.agentic.core.config import get_settings, is_strict_env

router = APIRouter(tags=["Health & Monitoring"])


def _check_tcp_port(host: str, port: int, timeout: float = 0.5) -> bool:
    """Synchronous compatibility probe; readiness offloads it to a worker."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


async def _acheck_tcp_port(host: str, port: int, timeout: float = 0.5) -> bool:
    return await asyncio.to_thread(_check_tcp_port, host, port, timeout)


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe indicating API process is alive."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> Response:
    """Readiness probe indicating API service and critical dependencies are ready to accept traffic."""
    settings = get_settings()
    checks: dict[str, str] = {}
    is_healthy = True

    strict = is_strict_env(settings)

    # 1. Database Check (Postgres or SQLite)
    if settings.use_postgres_checkpoint or strict:
        try:
            import psycopg

            def probe_postgres() -> None:
                with (
                    psycopg.connect(settings.postgres_uri, connect_timeout=1) as conn,
                    conn.cursor() as cur,
                ):
                    cur.execute("SELECT 1;")

            await asyncio.to_thread(probe_postgres)
            if settings.use_postgres_checkpoint:
                from src.agentic.memory.short_term import (
                    _global_checkpointer,
                    is_checkpointer_healthy,
                )

                if _global_checkpointer is not None and not await is_checkpointer_healthy():
                    checks["checkpointer"] = "unhealthy"
                    if strict:
                        is_healthy = False
                else:
                    checks["checkpointer"] = "ok"
            checks["database"] = "ok (postgres)"
        except Exception as e:
            checks["database"] = "down" if strict else f"down ({e.__class__.__name__})"
            if strict:
                is_healthy = False
    else:
        # Development / local SQLite check
        db_path = settings.project_root / "data" / "memory" / "long_term_store.db"
        checks["database"] = "ok (sqlite)" if db_path.parent.exists() else "degraded"

    # 2. Redis Check (Cache & State Clusters)
    if settings.enable_redis_rate_limit or settings.enable_redis_cache or settings.use_redis_checkpoint:
        try:
            parsed = urlparse(settings.redis_url)
            r_host = parsed.hostname or "localhost"
            r_port = parsed.port or 6379
            redis_ok = await _acheck_tcp_port(r_host, r_port, timeout=0.3)
            checks["redis"] = "ok" if redis_ok else "down"
            if not redis_ok and strict:
                is_healthy = False
        except Exception:
            checks["redis"] = "error"
            if strict:
                is_healthy = False

        if settings.redis_state_url and settings.redis_state_url != settings.redis_url:
            try:
                parsed_state = urlparse(settings.redis_state_url)
                s_host = parsed_state.hostname or "localhost"
                s_port = parsed_state.port or 6379
                state_ok = await _acheck_tcp_port(s_host, s_port, timeout=0.3)
                checks["redis_state"] = "ok" if state_ok else "down"
                if not state_ok and strict:
                    is_healthy = False
            except Exception:
                checks["redis_state"] = "error"
                if strict:
                    is_healthy = False

    if strict and settings.enable_redis_rate_limit:
        from src.agentic.api.middleware import is_rate_limiter_ready

        limiter_ok = await asyncio.to_thread(is_rate_limiter_ready)
        checks["rate_limiter"] = "ok" if limiter_ok else "unavailable"
        if not limiter_ok:
            is_healthy = False

    # 3. Model Gateway Check (LiteLLM)
    parsed_llm = urlparse(settings.litellm_base_url)
    llm_host = parsed_llm.hostname or "localhost"
    llm_port = parsed_llm.port or 4000
    llm_ok = await _acheck_tcp_port(llm_host, llm_port, timeout=0.3)
    checks["model_gateway"] = "ok" if llm_ok else "offline"
    if not llm_ok and settings.require_model_gateway_ready:
        is_healthy = False

    # 3b. Embedding service — a missing endpoint must never silently degrade
    # retrieval to constant mock vectors in production.
    parsed_embed = urlparse(settings.llama_embed_base_url)
    embed_host = parsed_embed.hostname or "localhost"
    embed_port = parsed_embed.port or 8081
    embed_ok = await _acheck_tcp_port(embed_host, embed_port, timeout=0.3)
    checks["embedding_service"] = "ok" if embed_ok else "offline"
    if settings.require_embedding_service and not embed_ok:
        is_healthy = False

    # 4. Vector Store (Qdrant)
    qdrant_ok = await _acheck_tcp_port(
        settings.qdrant_host, settings.qdrant_port, timeout=0.3
    )
    checks["vector_store"] = "ok" if qdrant_ok else "offline_or_local"
    if settings.enable_qdrant and not qdrant_ok:
        is_healthy = False

    overall_status = "ready" if is_healthy else "unhealthy"
    status_code = status.HTTP_200_OK if is_healthy else status.HTTP_503_SERVICE_UNAVAILABLE

    return JSONResponse(
        status_code=status_code,
        content={
            "status": overall_status,
            "environment": settings.environment,
            "checks": checks,
        },
    )


@router.get("/metrics")
async def metrics() -> Response:
    """Prometheus metrics endpoint (honours ENABLE_PROMETHEUS)."""
    if not get_settings().enable_prometheus:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": "Not Found"})
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)
