"""A2A Server for RAGAgent (Port 9101)."""

import argparse
import asyncio
import logging
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI

from src.agentic.a2a.cards import RAG_AGENT_CARD
from src.agentic.a2a.executor import create_a2a_server
from src.agentic.agents.rag.graph import get_rag_graph
from src.agentic.core.config import get_settings

logger = logging.getLogger(__name__)


async def _poll_rag_invalidation(stop: asyncio.Event) -> None:
    """Apply cross-replica RAG cache and HybridRetriever invalidations in this pod."""
    from src.agentic.cache.rag_cache import get_rag_cache

    cache = get_rag_cache()
    while not stop.is_set():
        try:
            await asyncio.to_thread(cache.check_invalidation_generation)
        except Exception as exc:  # observability must never crash the poller
            logger.warning("RAG invalidation poll failed: %s", exc)
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=5.0)


@asynccontextmanager
async def _rag_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the cross-replica invalidation poller on every RAGAgent pod."""
    stop_poller = asyncio.Event()
    poller = asyncio.create_task(_poll_rag_invalidation(stop_poller))
    try:
        yield
    finally:
        stop_poller.set()
        poller.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await poller


def _check_tcp(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def _rag_readiness() -> tuple[bool, dict[str, str]]:
    """Verify Qdrant and embedding service reachability before accepting RAG traffic."""
    settings = get_settings()
    checks: dict[str, str] = {}
    ok = True

    if settings.enable_qdrant:
        qdrant_ok = await asyncio.to_thread(_check_tcp, settings.qdrant_host, settings.qdrant_port)
        checks["vector_store"] = "ok" if qdrant_ok else "down"
        if not qdrant_ok:
            ok = False

    if settings.require_embedding_service:
        parsed = urlparse(settings.llama_embed_base_url)
        embed_ok = await asyncio.to_thread(
            _check_tcp,
            parsed.hostname or "localhost",
            parsed.port or (443 if parsed.scheme == "https" else 80),
        )
        checks["embedding_service"] = "ok" if embed_ok else "offline"
        if not embed_ok:
            ok = False

    return ok, checks


graph = get_rag_graph()
app = create_a2a_server(
    card=RAG_AGENT_CARD,
    graph=graph,
    readiness_check=_rag_readiness,
    lifespan=_rag_lifespan,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="RAGAgent A2A Server")
    parser.add_argument("--port", type=int, default=9101, help="Port to listen on")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host interface")
    args = parser.parse_args()

    uvicorn.run("src.agentic.agents.rag.server:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
