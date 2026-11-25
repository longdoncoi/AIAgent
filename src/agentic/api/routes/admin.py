"""Admin endpoints for dynamic configuration hot-reload and checkpoint maintenance."""

import asyncio
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from src.agentic.api.deps import require_role
from src.agentic.cache.rag_cache import get_rag_cache
from src.agentic.core.config import get_settings, is_strict_env
from src.agentic.llm.gateway import reload_gateway_config
from src.agentic.memory.data_lifecycle import (
    PurgeBackendUnavailable,
    PurgePermissionError,
    cleanup_stale_postgres_checkpoints,
    purge_user_data,
)
from src.agentic.memory.redis_saver import RedisSaver
from src.agentic.memory.short_term import get_checkpointer
from src.agentic.safety.policy import reload_policy_manager
from src.agentic.tools.mcp_client import reload_mcp_client_manager

logger = logging.getLogger(__name__)
_AUDIT_LOGGER = logging.getLogger("agentic.audit.data_lifecycle")
router = APIRouter(prefix="/api/v1/admin", tags=["Admin"])


class PurgeRequest(BaseModel):
    """Explicit confirmation body for destructive user-data purges."""

    confirm: bool = Field(default=False, description="Must be true to perform a real purge.")
    thread_id: str | None = Field(default=None, description="Optional public thread identifier.")
    dry_run: bool = Field(default=False, description="Preview deletions without mutating data.")


@router.post("/reload-configs")
async def reload_configs_endpoint(
    current_user: dict[str, Any] = Depends(require_role(["admin"])),
) -> dict[str, str]:
    """Dynamically reloads policies, model gateway parameters, and cache without downtime."""
    user_id = current_user.get("user_id", "admin")
    logger.info(f"Admin '{user_id}' requested system configuration hot-reload.")

    # 1. Reload policies
    reload_policy_manager()
    reload_mcp_client_manager()

    # 2. Reset gateway circuit breaker & models
    reload_gateway_config()

    # 3. Clear transient RAG cache (blocking Redis I/O off the event loop)
    await asyncio.to_thread(get_rag_cache().clear)

    return {
        "status": "ok",
        "message": "Policies, Model Gateway routing, and RAG cache reloaded successfully.",
    }


@router.post("/cleanup-checkpoints")
async def cleanup_checkpoints_endpoint(
    max_idle_seconds: int = Query(86400 * 30, ge=3600, le=86400 * 365),
    current_user: dict[str, Any] = Depends(require_role(["admin"])),
) -> dict[str, Any]:
    """Evicts expired or inactive thread checkpoints from persistent stores."""
    user_id = current_user.get("user_id", "admin")
    logger.info(f"Admin '{user_id}' requested checkpoint cleanup (max_idle={max_idle_seconds}s).")

    checkpointer = get_checkpointer()
    settings = get_settings()
    cleaned = 0
    if isinstance(checkpointer, RedisSaver):
        cleaned = await checkpointer.acleanup_inactive_threads(
            max_idle_seconds=max_idle_seconds
        )
    elif settings.use_postgres_checkpoint or is_strict_env(settings):
        try:
            cleaned = await asyncio.to_thread(
                cleanup_stale_postgres_checkpoints, max_idle_seconds
            )
        except PurgeBackendUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc

    return {
        "status": "ok",
        "cleaned_threads": cleaned,
        "max_idle_seconds": max_idle_seconds,
    }


@router.post("/users/{user_id}/purge")
async def purge_user_data_endpoint(
    user_id: str,
    payload: PurgeRequest | None = None,
    confirm: bool = Query(
        False, description="Explicit confirmation; required when no body is provided."
    ),
    dry_run: bool = Query(False, description="Preview deletions without mutating data."),
    current_user: dict[str, Any] = Depends(require_role(["admin"])),
) -> dict[str, Any]:
    """Right-to-be-Forgotten: erase all persisted data for a principal.

    Destructive purges require explicit confirmation via the request body
    (``confirm: true``) or the ``?confirm=true`` query parameter. The caller's
    identity is recorded in the audit log for compliance traceability.
    """
    requested = payload or PurgeRequest()
    is_confirmed = requested.confirm or confirm
    preview = requested.dry_run or dry_run
    if not preview and not is_confirmed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Purge requires explicit confirmation: set 'confirm=true' in the body "
                "or as a query parameter, or use 'dry_run=true' to preview."
            ),
        )

    admin_id = str(current_user.get("user_id", "admin"))
    _AUDIT_LOGGER.info(
        "DATA_PURGE_REQUEST admin=%s target=%s thread_id=%s dry_run=%s",
        admin_id,
        user_id,
        requested.thread_id,
        preview,
    )
    logger.info(
        "Admin '%s' requested data purge for '%s' (dry_run=%s).", admin_id, user_id, preview
    )
    try:
        report = await purge_user_data(
            user_id, requested.thread_id, actor_id=admin_id, dry_run=preview
        )
    except PurgePermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except PurgeBackendUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return report.to_dict()


@router.post("/rag/cdc")
async def trigger_rag_cdc_endpoint(
    payload: dict[str, Any],
    current_user: dict[str, Any] = Depends(require_role(["admin"])),
) -> dict[str, Any]:
    """Trigger durable CDC delta RAG ingestion via Temporal (or direct activity fallback)."""
    from src.agentic.workflows.rag_workflow import (
        DeltaDocumentEvent,
        RealtimeKnowledgeCdcWorkflow,
        ingest_delta_document_activity,
    )

    event = DeltaDocumentEvent(**payload)
    settings = get_settings()
    try:
        from temporalio.client import Client

        client = await Client.connect(
            settings.temporal_host, namespace=settings.temporal_namespace
        )
        handle = await client.start_workflow(
            RealtimeKnowledgeCdcWorkflow.run,
            event.model_dump(),
            id=f"rag-cdc-{event.doc_id}-{uuid.uuid4().hex[:8]}",
            task_queue=settings.temporal_task_queue,
        )
        return {"status": "dispatched", "workflow_id": handle.id, "doc_id": event.doc_id}
    except Exception as exc:
        if is_strict_env(settings) and settings.use_temporal_dispatch:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Temporal CDC dispatch unavailable: {exc}",
            ) from exc
        result = await ingest_delta_document_activity(event.model_dump())
        return {"status": "completed_inline", **result}
