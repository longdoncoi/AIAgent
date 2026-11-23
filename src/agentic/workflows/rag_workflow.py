"""Temporal Workflow and Activities for Durable, Asynchronous RAG Ingestion."""

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from src.agentic.core.config import get_settings
from src.agentic.rag.ingestion import ingest_documents


class DeltaDocumentEvent(BaseModel):
    doc_id: str = Field(..., description="Unique document ID")
    file_path: str = Field(..., description="File path relative to data/raw")
    action: str = Field(default="upsert", description="upsert | delete")
    content: str = Field(default="", description="Text content for delta indexing")
    metadata: dict[str, Any] = Field(default_factory=dict)


@activity.defn
async def ingest_delta_document_activity(event_data: dict[str, Any]) -> dict[str, Any]:
    """Incremental delta document ingestion with immediate RAG cache invalidation."""
    from src.agentic.cache.rag_cache import get_rag_cache
    from src.agentic.rag.retriever import reset_hybrid_retriever

    settings = get_settings()
    doc_id = str(event_data.get("doc_id", "doc_delta"))
    action = str(event_data.get("action", "upsert"))
    content = str(event_data.get("content", ""))
    metadata = event_data.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("Delta metadata must be an object.")
    tenant_id = str(metadata.get("tenant_id") or "")
    if settings.environment == "production" and not tenant_id:
        raise ValueError("Production delta ingestion requires metadata.tenant_id.")

    # Invalidate cache selectively to preserve unrelated document cache
    rag_cache = get_rag_cache()
    file_path = str(event_data.get("file_path", f"delta_{doc_id}.md"))
    raw_root = (settings.project_root / "data" / "raw").resolve()
    target = (raw_root / file_path).resolve()
    sidecar = target.with_suffix(target.suffix + ".metadata.json")
    try:
        target.relative_to(raw_root)
    except ValueError as exc:
        raise ValueError("Delta document path escapes the configured raw-data root.") from exc
    invalidated_count = rag_cache.invalidate_document(doc_id)
    if file_path:
        invalidated_count += rag_cache.invalidate_by_source(file_path)
    if invalidated_count == 0:
        # Fallback to clear if doc_id was not explicitly in keys
        rag_cache.clear()

    # If content is provided, save to raw folder
    if action == "upsert" and content:
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_text, content, encoding="utf-8")
        trusted_metadata = {
            **metadata,
            "visibility": "tenant" if tenant_id else "shared",
        }
        if tenant_id:
            trusted_metadata["tenant_id"] = tenant_id
        await asyncio.to_thread(
            sidecar.write_text,
            json.dumps(trusted_metadata, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
    elif action == "delete":
        if target.exists() and target.is_file():
            await asyncio.to_thread(target.unlink)
        if sidecar.exists() and sidecar.is_file():
            await asyncio.to_thread(sidecar.unlink)
    elif action != "upsert":
        raise ValueError("Delta action must be either 'upsert' or 'delete'.")

    # Rebuild persisted storage before swapping the process-local reader.
    await asyncio.to_thread(
        ingest_documents,
        raw_root,
        settings.project_root / "data" / "index",
    )
    reset_hybrid_retriever()

    return {
        "status": "completed",
        "action": action,
        "doc_id": doc_id,
        "cache_invalidated": True,
    }


@workflow.defn
class RealtimeKnowledgeCdcWorkflow:
    """Durable Temporal workflow processing CDC document stream with automated retries."""

    @workflow.run
    async def run(self, event_data: dict[str, Any]) -> dict[str, Any]:
        retry_policy = RetryPolicy(
            initial_interval=timedelta(seconds=2),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(seconds=30),
            maximum_attempts=3,
        )

        return await workflow.execute_activity(
            ingest_delta_document_activity,
            args=[event_data],
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=retry_policy,
        )


@activity.defn
async def ingest_documents_activity(
    data_dir: str | None = None, index_dir: str | None = None
) -> dict[str, Any]:
    """Activity performing parsing, hierarchical chunking, and index persistence."""
    settings = get_settings()
    raw_path = Path(data_dir) if data_dir else (settings.project_root / "data" / "raw")
    index_path = Path(index_dir) if index_dir else (settings.project_root / "data" / "index")

    leaf_count = await asyncio.to_thread(
        ingest_documents, raw_data_dir=raw_path, index_persist_dir=index_path
    )
    return {
        "status": "completed",
        "persist_dir": str(index_path),
        "nodes_indexed": leaf_count,
    }


@workflow.defn
class RagIngestionWorkflow:
    """Durable Temporal workflow managing asynchronous ingestion with automated retries."""

    @workflow.run
    async def run(
        self, data_dir: str | None = None, index_dir: str | None = None
    ) -> dict[str, Any]:
        retry_policy = RetryPolicy(
            initial_interval=timedelta(seconds=5),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=1),
            maximum_attempts=3,
        )

        return await workflow.execute_activity(
            ingest_documents_activity,
            args=[data_dir, index_dir],
            start_to_close_timeout=timedelta(minutes=15),
            retry_policy=retry_policy,
        )
