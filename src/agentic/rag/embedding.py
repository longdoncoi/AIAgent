"""Shared, fail-closed embedding-service resolution for RAG ingestion and retrieval.

Both :mod:`src.agentic.rag.retriever` and :mod:`src.agentic.rag.ingestion` use
:func:`configure_embedding_model` so there is exactly one source of truth for
the embedding endpoint. The old code probed a hardcoded ``localhost:8081`` and
silently fell back to ``MockEmbedding``; mock embeddings return a constant
vector, making every cosine similarity exactly 1.0 -- i.e. silently broken
retrieval in production.

Production rule: when ``settings.require_embedding_service`` is True (the
production default), an unreachable/invalid embedding service raises
``RuntimeError`` instead of degrading to mock. The active provider is exported
via the ``agent_rag_embed_provider{type="real|mock"}`` gauge.
"""

from __future__ import annotations

import logging
import socket
import threading
from typing import Any
from urllib.parse import urlparse

from src.agentic.core.config import Settings, get_settings
from src.agentic.observability.metrics import RAG_EMBED_PROVIDER

logger = logging.getLogger(__name__)


def _set_embed_provider(provider: str) -> None:
    """Publish the selected provider (shared metric, info-style counter)."""
    RAG_EMBED_PROVIDER.labels(type=provider).inc()

_EMBEDDING_LOCK = threading.Lock()
_configured_provider: str | None = None

_EMBED_API_KEY = "sk-embed-dummy"  # Accepted by the local llama.cpp embedding server.


def resolve_embed_endpoint(base_url: str, settings: Settings | None = None) -> tuple[str, int]:
    """Parse (host, port) from ``settings.llama_embed_base_url``.

    Never probe a hardcoded host: the configured URL is authoritative, including
    in Kubernetes where it points at an internal service DNS name.
    """
    effective_settings = settings or get_settings()
    url = base_url or effective_settings.llama_embed_base_url
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise RuntimeError(
            "Invalid llama_embed_base_url: no hostname could be parsed from "
            f"{url!r}; refusing to guess an embedding endpoint."
        )
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return host, port


def is_embedding_service_reachable(base_url: str, timeout: float = 0.3) -> bool:
    """TCP-probe the *configured* embedding endpoint (cheap, bounded timeout)."""
    host, port = resolve_embed_endpoint(base_url)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError as exc:
        logger.warning("Embedding service at %s:%s is unreachable: %s", host, port, exc)
        return False


def get_configured_embedding_provider() -> str | None:
    """Return the provider selected by the last :func:`configure_embedding_model` call."""
    with _EMBEDDING_LOCK:
        return _configured_provider


def invalidate_embedding_provider_cache() -> None:
    """Forget the cached provider so a config reload / index rebuild re-resolves it."""
    global _configured_provider
    with _EMBEDDING_LOCK:
        _configured_provider = None


def configure_embedding_model(*, force: bool = False) -> str:
    """Configure LlamaIndex's global embedding model exactly once.

    Returns ``"real"`` (configured llama.cpp/OpenAI-compatible embedding
    service) or ``"mock"`` (explicit development-only degradation).

    Raises:
        RuntimeError: If the service is required (production) but unreachable,
            or if the configured URL is invalid. There is intentionally no
            silent fallback in this case.
    """
    global _configured_provider
    settings = get_settings()

    with _EMBEDDING_LOCK:
        if _configured_provider is not None and not force:
            return _configured_provider

        base_url = settings.llama_embed_base_url
        host, port = resolve_embed_endpoint(base_url, settings)

        if is_embedding_service_reachable(base_url):
            from llama_index.embeddings.openai import OpenAIEmbedding

            embedding_model: Any = OpenAIEmbedding(
                api_base=base_url,
                api_key=_EMBED_API_KEY,
                model_name=settings.default_embedding_model,
            )
            provider = "real"
            logger.info(
                "Using real embedding service at %s:%s (model=%s).",
                host,
                port,
                settings.default_embedding_model,
            )
        elif settings.require_embedding_service:
            # Fail closed: a mock embedder makes cosine similarity == 1.0 for
            # every document, which silently destroys retrieval quality.
            raise RuntimeError(
                f"Embedding service at {host}:{port} is unreachable but "
                "require_embedding_service=True; refusing to fall back to "
                "MockEmbedding. Check llama_embed_base_url / service health."
            )
        else:
            from llama_index.core.embeddings.mock_embed_model import MockEmbedding

            embedding_model = MockEmbedding(embed_dim=settings.default_embedding_dim)
            provider = "mock"
            logger.warning(
                "Embedding service at %s:%s is unreachable; using MockEmbedding. "
                "Retrieval results are NOT meaningful in this mode (development only).",
                host,
                port,
            )

        from llama_index.core import Settings as LlamaSettings

        LlamaSettings.embed_model = embedding_model
        _set_embed_provider(provider)
        _configured_provider = provider
        return provider
