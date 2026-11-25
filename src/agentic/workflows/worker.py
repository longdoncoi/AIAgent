"""Temporal Worker runner for agentic background tasks."""

import asyncio
import contextlib
import logging
import os
import signal
from datetime import timedelta

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


async def _start_health_server(
    ready_flag: dict[str, bool], port: int = 9105
) -> asyncio.AbstractServer:
    """Lightweight asyncio health/readiness server for Kubernetes probes."""

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await asyncio.wait_for(reader.read(1024), timeout=1.0)
            req_line = data.decode("utf-8", errors="ignore").split("\r\n", 1)[0]
            if "/readyz" in req_line:
                ok = bool(ready_flag.get("ready"))
                status_line = "200 OK" if ok else "503 Service Unavailable"
                body = b'{"status":"ready"}' if ok else b'{"status":"starting"}'
            else:
                status_line = "200 OK"
                body = b'{"status":"ok"}'
            header = (
                f"HTTP/1.1 {status_line}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            writer.write(header + body)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    return await asyncio.start_server(_handle, host="0.0.0.0", port=port)  # nosec B104


async def run_worker() -> None:
    settings = get_settings()
    ready_flag = {"ready": False}
    health_port = int(os.environ.get("TEMPORAL_WORKER_HEALTH_PORT", "9105"))
    health_server: asyncio.AbstractServer | None = None
    logger.info(f"Connecting to Temporal at {settings.temporal_host}...")
    try:
        health_server = await _start_health_server(ready_flag, port=health_port)
        tls_enabled = os.environ.get("TEMPORAL_TLS_ENABLED", "").strip().lower() == "true"
        client = await Client.connect(
            settings.temporal_host,
            namespace=settings.temporal_namespace,
            tls=tls_enabled,
        )
        worker = Worker(
            client,
            task_queue=settings.temporal_task_queue,
            workflows=[AgentExecutionWorkflow, RagIngestionWorkflow, RealtimeKnowledgeCdcWorkflow],
            activities=[
                execute_plan_step,
                ingest_documents_activity,
                ingest_delta_document_activity,
            ],
            graceful_shutdown_timeout=timedelta(seconds=30),
        )
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop_event.set)
        ready_flag["ready"] = True
        logger.info(f"Temporal worker started on task queue '{settings.temporal_task_queue}'.")
        worker_task = asyncio.create_task(worker.run())
        stop_wait = asyncio.create_task(stop_event.wait())
        done, _ = await asyncio.wait(
            [worker_task, stop_wait], return_when=asyncio.FIRST_COMPLETED
        )
        if stop_wait in done:
            ready_flag["ready"] = False
            await worker.shutdown()
            await worker_task
        else:
            stop_wait.cancel()
            await worker_task
    except Exception as e:
        ready_flag["ready"] = False
        logger.error("Could not start Temporal worker: %s", e)
        raise
    finally:
        ready_flag["ready"] = False
        if health_server is not None:
            health_server.close()
            await health_server.wait_closed()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
