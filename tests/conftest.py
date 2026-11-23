"""Pytest fixtures and test environment setup."""

import os
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Force test configuration
os.environ["ENVIRONMENT"] = "test"
os.environ["DEBUG"] = "false"
os.environ["USE_POSTGRES_CHECKPOINT"] = "false"
os.environ["ENABLE_GUARDRAILS"] = "true"
os.environ["PII_REDACTION_ENABLED"] = "true"
os.environ["A2A_RETRY_BACKOFF_BASE_SECONDS"] = "0.001"
os.environ["A2A_RETRY_BACKOFF_MAX_SECONDS"] = "0.01"

from langgraph.checkpoint.memory import MemorySaver

from src.agentic.api.main import app
from src.agentic.orchestrator.graph import create_supervisor_graph


@pytest.fixture
def memory_checkpointer() -> MemorySaver:
    """Provides a fresh in-memory checkpointer for each test."""
    return MemorySaver()


@pytest.fixture
def supervisor_test_graph(memory_checkpointer: MemorySaver):
    """Provides a compiled supervisor graph configured with an in-memory checkpointer."""
    return create_supervisor_graph(checkpointer=memory_checkpointer)


@pytest.fixture
async def async_client():
    """Async HTTP test client for L1 FastAPI endpoints."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
