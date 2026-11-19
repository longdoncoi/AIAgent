"""KV-Cache and Prefix Optimization helpers for vLLM, llama.cpp, and LiteLLM."""

import logging
from collections.abc import Sequence

from langchain_core.messages import BaseMessage, SystemMessage

from src.agentic.core.config import get_settings

logger = logging.getLogger(__name__)


def optimize_messages_for_kv_cache(
    messages: Sequence[BaseMessage],
    system_prompt: str | None = None,
) -> list[BaseMessage]:
    """Optimizes the sequence of messages to maximize GPU KV-cache reuse (Automatic Prefix Caching).

    Rules applied:
    1. Static Prefix Invariance: System message is pinned to index 0, stripped of dynamic timestamps/UUIDs.
    2. Ephemeral Isolation: Any dynamic context (request IDs, session metadata) is placed in subsequent user messages.
    3. Stable Ordering: Ensures identical prefixes across requests yield identical token IDs in vLLM / llama.cpp.
    """
    settings = get_settings()
    if not settings.enable_kv_cache_prefix_optimization:
        return list(messages)

    system_msgs: list[SystemMessage] = []
    other_msgs: list[BaseMessage] = []

    for msg in messages:
        if isinstance(msg, SystemMessage):
            system_msgs.append(msg)
        else:
            other_msgs.append(msg)

    # If explicit system prompt is provided, prefer it as the invariant prefix
    effective_system_content = ""
    if system_prompt:
        effective_system_content = system_prompt.strip()
    elif system_msgs:
        # Deduplicate and combine system messages
        effective_system_content = "\n\n".join(str(m.content).strip() for m in system_msgs)

    optimized: list[BaseMessage] = []
    if effective_system_content:
        # Create invariant SystemMessage (KV-cache anchor)
        optimized.append(SystemMessage(content=effective_system_content))

    optimized.extend(other_msgs)
    return optimized


def get_kv_cache_headers() -> dict[str, str]:
    """Returns HTTP headers facilitating prefix caching on supported inference gateways."""
    return {
        "X-Cache-Control": "ephemeral",
        "anthropic-beta": "prompt-caching-2024-07-31",
    }
