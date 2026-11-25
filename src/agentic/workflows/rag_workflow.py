"""Temporal Workflow and Activities for Durable, Asynchronous RAG Ingestion."""

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from src.agentic.core.config import get_settings
    from src.agentic.rag.ingestion import delete_source, ingest_documents, ingest_single_document


class DeltaDocumentEvent(BaseModel):
    doc_id: str = Field(..., description="Unique document ID")
    file_path: str = Field(..., description="File path relative to data/raw")
    action: str = Field(default="upsert", description="upsert | delete")
    content: str = Field(default="", description="Text content for delta indexing")
    metadata: dict[str, Any] = Field(default_factory=dict)


@activity.defn
async def ingest_delta_document_activity(event_data: dict[str, Any]) -> dict[str, Any]:
    """Incremental delta document ingestion with post-rebuild RAG cache invalidation."""
    from src.agentic.cache.rag_cache import get_rag_cache
    from src.agentic.rag.retriever import reset_hybrid_retriever

    settings = get_settings()
    doc_id = str(event_data.get("doc_id", "doc_delta"))
    action = str(event_data.get("action", "upsert"))
    content = str(event_data.get("content", ""))

    file_path = str(event_data.get("file_path", f"delta_{doc_id}.md"))
    raw_root = (settings.project_root / "data" / "raw").resolve()
    index_dir = settings.project_root / "data" / "index"
    target = (raw_root / file_path).resolve()
    try:
        target.relative_to(raw_root)
    except ValueError as exc:
        raise ValueError("Delta document path escapes the configured raw-data root.") from exc

    if action not in {"upsert", "delete"}:
        raise ValueError("Delta action must be either 'upsert' or 'delete'.")

    # Mutate corpus and vector/docstore state BEFORE invalidating caches so
    # concurrent readers cannot refill the cache with pre-CDC stale data.
    if action == "upsert" and content:
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_text, content, encoding="utf-8")
    elif action == "delete":
        if target.exists() and target.is_file():
            await asyncio.to_thread(target.unlink)
        await asyncio.to_thread(delete_source, file_path, index_persist_dir=index_dir)
        await asyncio.to_thread(delete_source, str(target), index_persist_dir=index_dir)

    event_meta = event_data.get("metadata")
    meta_dict = event_meta if isinstance(event_meta, dict) else {}
    tenant_id = str(meta_dict.get("tenant_id")).strip() if meta_dict.get("tenant_id") else None
    acl = [str(x) for x in meta_dict["acl"]] if isinstance(meta_dict.get("acl"), list) else None
    department = (
        str(meta_dict.get("department")).strip() if meta_dict.get("department") else None
    )
    allowed_roles = (
        [str(x) for x in meta_dict["allowed_roles"]]
        if isinstance(meta_dict.get("allowed_roles"), list)
        else None
    )

    if action == "upsert" and target.is_file():
        await asyncio.to_thread(
            ingest_single_document,
            target,
            index_persist_dir=index_dir,
            tenant_id=tenant_id,
            acl=acl,
            department=department,
            allowed_roles=allowed_roles,
        )
    reset_hybrid_retriever()

    # Selective invalidation + cross-process generation bump AFTER new index is live.
    rag_cache = get_rag_cache()
    invalidated_count = await asyncio.to_thread(rag_cache.invalidate_document, doc_id)
    if file_path:
        invalidated_count += await asyncio.to_thread(rag_cache.invalidate_by_source, file_path)

    publisher = getattr(rag_cache, "publish_invalidation", None)
    if publisher is not None:
        await asyncio.to_thread(publisher, doc_id, file_path)

    return {
        "status": "completed",
        "action": action,
        "doc_id": doc_id,
        "cache_invalidated": True,
        "invalidated_entries": invalidated_count,
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
