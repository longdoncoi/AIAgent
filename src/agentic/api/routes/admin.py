"""Admin endpoints for dynamic configuration hot-reload and checkpoint maintenance."""

import logging
from typing import Any

from fastapi import APIRouter, Depends

from src.agentic.api.deps import require_role
from src.agentic.cache.rag_cache import get_rag_cache
from src.agentic.llm.gateway import reload_gateway_config
from src.agentic.memory.redis_saver import RedisSaver
from src.agentic.memory.short_term import get_checkpointer
from src.agentic.safety.policy import reload_policy_manager
from src.agentic.tools.mcp_client import reload_mcp_client_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/admin", tags=["Admin"])


@router.post("/reload-configs")
async def reload_configs_endpoint(
    current_user: dict[str, Any] = Depends(require_role(["admin"])),
) -> dict[str, str]:
    """Dynamically reloads policies, model gateway parameters, and cache without downtime."""
    user_id = current_user.get("user_id", "admin")
    logger.info(f"Admin '{user_id}' requested system configuration hot-reload.")

    # 1. Reload policies
    policy_mgr = reload_policy_manager()
    reload_mcp_client_manager()

    # 2. Reset gateway circuit breaker & models
    reload_gateway_config()

    # 3. Clear transient RAG cache
    get_rag_cache().clear()

    return {
        "status": "ok",
        "message": "Policies, Model Gateway routing, and RAG cache reloaded successfully.",
        "loaded_tools_count": str(len(policy_mgr.policy_data.get("tools", {}))),
    }


@router.post("/cleanup-checkpoints")
async def cleanup_checkpoints_endpoint(
    max_idle_seconds: int = 86400 * 30,
    current_user: dict[str, Any] = Depends(require_role(["admin"])),
) -> dict[str, Any]:
    """Evicts expired or inactive thread checkpoints from persistent stores."""
    user_id = current_user.get("user_id", "admin")
    logger.info(f"Admin '{user_id}' requested checkpoint cleanup (max_idle={max_idle_seconds}s).")

    checkpointer = get_checkpointer()
    cleaned = 0
    if isinstance(checkpointer, RedisSaver):
        cleaned = await checkpointer.acleanup_inactive_threads(
            max_idle_seconds=max_idle_seconds
        )

    return {
        "status": "ok",
        "cleaned_threads": cleaned,
        "max_idle_seconds": max_idle_seconds,
    }
