"""Consolidation node distilling conversation takeaways into long-term memory."""

import logging
from typing import Any

from src.agentic.memory.long_term import get_long_term_store

logger = logging.getLogger(__name__)


async def consolidate_session_memory(
    thread_id: str, goal: str, final_answer: str, metadata: dict[str, Any] | None = None
) -> None:
    """Extracts summary and writes episodic memory entry upon session completion."""
    try:
        store = get_long_term_store()
        summary = f"Goal: {goal}\nOutcome: {final_answer[:300]}..."
        store.add_episodic(
            task_id=thread_id,
            summary=summary,
            metadata=metadata or {},
        )
        logger.debug(f"Consolidated episodic memory for thread: {thread_id}")
    except Exception as e:
        logger.warning(f"Failed to consolidate memory for thread {thread_id}: {e}")
