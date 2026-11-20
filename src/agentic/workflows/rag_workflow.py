"""Temporal Workflow and Activities for Durable, Asynchronous RAG Ingestion."""

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
    from src.agentic.rag.retriever import get_hybrid_retriever

    settings = get_settings()
    doc_id = str(event_data.get("doc_id", "doc_delta"))
    action = str(event_data.get("action", "upsert"))
    content = str(event_data.get("content", ""))

    # Invalidate cache selectively to preserve unrelated document cache
    rag_cache = get_rag_cache()
    file_path = str(event_data.get("file_path", f"delta_{doc_id}.md"))
    invalidated_count = rag_cache.invalidate_document(doc_id)
    if file_path:
        invalidated_count += rag_cache.invalidate_by_source(file_path)
    if invalidated_count == 0:
        # Fallback to clear if doc_id was not explicitly in keys
        rag_cache.clear()

    # If content is provided, save to raw folder
    if action == "upsert" and content:
        file_path = str(event_data.get("file_path", f"delta_{doc_id}.md"))
        target = settings.project_root / "data" / "raw" / file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)

    # Re-trigger hybrid retriever storage reload
    retriever = get_hybrid_retriever()
    retriever._load_storage()

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

    leaf_count = ingest_documents(raw_data_dir=raw_path, index_persist_dir=index_path)
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
