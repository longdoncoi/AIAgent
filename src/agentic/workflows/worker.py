"""Temporal Worker runner for agentic background tasks."""

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from src.agentic.core.config import get_settings
from src.agentic.workflows.agent_workflow import AgentExecutionWorkflow, execute_plan_step
from src.agentic.workflows.rag_workflow import (
    RagIngestionWorkflow,
    RealtimeKnowledgeCdcWorkflow,
    ingest_delta_document_activity,
    ingest_documents_activity,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("temporal-worker")


async def run_worker() -> None:
    settings = get_settings()
    logger.info(f"Connecting to Temporal at {settings.temporal_host}...")
    try:
        client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
        worker = Worker(
            client,
            task_queue=settings.temporal_task_queue,
            workflows=[AgentExecutionWorkflow, RagIngestionWorkflow, RealtimeKnowledgeCdcWorkflow],
            activities=[
                execute_plan_step,
                ingest_documents_activity,
                ingest_delta_document_activity,
            ],
        )
        logger.info(f"Temporal worker started on task queue '{settings.temporal_task_queue}'.")
        await worker.run()
    except Exception as e:
        logger.error("Could not start Temporal worker: %s", e)
        raise


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
