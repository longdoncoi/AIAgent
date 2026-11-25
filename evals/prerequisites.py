"""Lightweight, bounded prerequisite probes shared by evaluation entrypoints."""

from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

from src.agentic.core.config import get_settings

PROXY_BANNER = "heuristic proxy — not a release-quality measurement"


def _tcp_reachable(host: str, port: int, timeout: float = 0.3) -> bool:
    """Return whether a host accepts a TCP connection within ``timeout``."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _url_endpoint_reachable(url: str, timeout: float = 0.3) -> bool:
    """Return whether an HTTP(S) endpoint accepts a bounded TCP connection."""
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return _tcp_reachable(host, port, timeout=timeout)


def gateway_reachable() -> bool:
    """Fast TCP probe of the configured LiteLLM gateway endpoint."""
    return _url_endpoint_reachable(get_settings().litellm_base_url)


def llm_judge_available() -> bool:
    """True only when a real LLM judge can be used."""
    return os.environ.get("SKIP_LLM_JUDGE") != "true" and gateway_reachable()


def heuristic_mode_active() -> bool:
    """True when the environment forces heuristic planners or judges."""
    return (
        os.environ.get("FAST_HEURISTIC_PLANNER") == "true"
        or os.environ.get("SKIP_LLM_JUDGE") == "true"
        or not gateway_reachable()
    )


def release_blockers() -> list[str]:
    """List hard prerequisites for a release-quality evaluation run."""
    blockers: list[str] = []
    settings = get_settings()
    if not gateway_reachable():
        blockers.append("LLM gateway unreachable — no real generation or LLM judging possible")
    if settings.require_embedding_service and not _url_endpoint_reachable(
        settings.llama_embed_base_url
    ):
        blockers.append(
            "embedding service unreachable — production retrieval cannot be measured"
        )
    if settings.enable_qdrant and not _tcp_reachable(
        settings.qdrant_host,
        settings.qdrant_port,
    ):
        blockers.append("Qdrant unreachable — production vector retrieval cannot be measured")
    if os.environ.get("SKIP_LLM_JUDGE") == "true":
        blockers.append("SKIP_LLM_JUDGE=true — LLM judge explicitly disabled")
    if os.environ.get("FAST_HEURISTIC_PLANNER") == "true":
        blockers.append("FAST_HEURISTIC_PLANNER=true — planner routing is heuristic")
    if os.environ.get("ENVIRONMENT") == "test" or settings.environment == "test":
        blockers.append("ENVIRONMENT=test — release gate must run against production config")
    return blockers
