"""Health, deep readiness, and Prometheus metrics endpoints."""

import socket
from urllib.parse import urlparse

from fastapi import APIRouter, Response, status
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from src.agentic.core.config import get_settings

router = APIRouter(tags=["Health & Monitoring"])


def _check_tcp_port(host: str, port: int, timeout: float = 0.5) -> bool:
    """Fast TCP socket probe for backing infrastructure services."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


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

    # 1. Database Check (Postgres or SQLite)
    if settings.use_postgres_checkpoint or settings.environment == "production":
        try:
            import psycopg
            with psycopg.connect(settings.postgres_uri, connect_timeout=1) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1;")
            checks["database"] = "ok (postgres)"
        except Exception as e:
            checks["database"] = f"down ({e.__class__.__name__})"
            if settings.environment == "production":
                is_healthy = False
    else:
        # Development / local SQLite check
        db_path = settings.project_root / "data" / "memory" / "long_term_store.db"
        checks["database"] = "ok (sqlite)" if db_path.parent.exists() else "degraded"

    # 2. Redis Check (if enabled)
    if settings.enable_redis_rate_limit or settings.enable_redis_cache:
        try:
            parsed = urlparse(settings.redis_url)
            r_host = parsed.hostname or "localhost"
            r_port = parsed.port or 6379
            checks["redis"] = "ok" if _check_tcp_port(r_host, r_port, timeout=0.3) else "down"
        except Exception:
            checks["redis"] = "error"

    # 3. Model Gateway Check (LiteLLM)
    parsed_llm = urlparse(settings.litellm_base_url)
    llm_host = parsed_llm.hostname or "localhost"
    llm_port = parsed_llm.port or 4000
    checks["model_gateway"] = "ok" if _check_tcp_port(llm_host, llm_port, timeout=0.3) else "offline_or_fallback"

    # 4. Vector Store (Qdrant)
    checks["vector_store"] = "ok" if _check_tcp_port(settings.qdrant_host, settings.qdrant_port, timeout=0.3) else "offline_or_local"

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
    """Prometheus metrics endpoint."""
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)
