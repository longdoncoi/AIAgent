"""Integration tests for production protocol, identity, and replica boundaries."""

from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from a2a.helpers import new_task
from a2a.server.context import ServerCallContext
from a2a.server.tasks import DatabaseTaskStore
from a2a.types import TaskState
from fastapi import FastAPI, HTTPException
from langchain_core.messages import AIMessage
from sqlalchemy.ext.asyncio import create_async_engine

from src.agentic.a2a.cards import CHAT_AGENT_CARD
from src.agentic.a2a.client import A2AClient
from src.agentic.a2a.executor import create_a2a_server
from src.agentic.core.config import get_settings
from src.agentic.safety.capabilities import issue_tool_capability
from src.agentic.tools.execution_context import get_tool_execution_context
from src.agentic.tools.servers.security import authorize_tool_execution, secure_mcp_app


class _Graph:
    async def ainvoke(self, _: object) -> dict[str, list[AIMessage]]:
        return {"messages": [AIMessage(content="official protocol response")]}


@pytest.mark.asyncio
async def test_official_a2a_card_and_jsonrpc_round_trip() -> None:
    app = create_a2a_server(CHAT_AGENT_CARD, _Graph())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:9100") as http:
        card_response = await http.get("/.well-known/agent-card.json")
        assert card_response.status_code == 200
        card = card_response.json()
        assert card["name"] == "chat_agent"
        assert card["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"
        assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
        assert card["capabilities"]["streaming"] is True

        client = A2AClient()
        client.settings = client.settings.model_copy(
            update={"allow_local_agent_fallback": False, "a2a_max_retries": 1}
        )
        client._http_client = http
        response = await client.call_agent("chat", "step-a2a-1", "hello")
        assert response.status == "completed"
        assert response.output == "official protocol response"
        client._http_client = None


@pytest.mark.asyncio
async def test_a2a_network_boundary_fails_closed_and_accepts_bound_capability() -> None:
    settings = get_settings()
    with (
        patch.object(settings, "require_authentication", True),
        patch.object(settings, "s2s_internal_token", "integration-s2s-token"),
    ):
        app = create_a2a_server(CHAT_AGENT_CARD, _Graph())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:9100") as http:
            missing = await http.post("/a2a", json={})
            assert missing.status_code == 401

            invalid = await http.post(
                "/a2a",
                json={},
                headers={
                    "X-Internal-Service-Token": "integration-s2s-token",
                    "X-Agent-Name": "chat_agent",
                    "X-Step-ID": "step-secure",
                    "X-Owner-ID": "tenant-a",
                    "X-Run-ID": "run-a",
                    "X-Tool-Capability": "invalid",
                },
            )
            assert invalid.status_code == 403

            capability = issue_tool_capability(
                run_id="run-a",
                step_id="step-secure",
                owner_id="tenant-a",
                agent_name="chat_agent",
                allowed_tools=[],
            )
            client = A2AClient()
            client.settings = settings
            client._http_client = http
            response = await client.call_agent(
                "chat",
                "step-secure",
                "hello",
                metadata={
                    "run_id": "run-a",
                    "owner_id": "tenant-a",
                    "approval_capability": capability,
                },
            )
            assert response.status == "completed"
            client._http_client = None


@pytest.mark.asyncio
async def test_two_task_store_replicas_share_persistent_state(tmp_path: Path) -> None:
    database = (tmp_path / "a2a-replica-test.db").as_posix()
    engine_a = create_async_engine(f"sqlite+aiosqlite:///{database}")
    engine_b = create_async_engine(f"sqlite+aiosqlite:///{database}")

    def owner(context: ServerCallContext) -> str:
        return context.tenant

    store_a = DatabaseTaskStore(engine_a, create_table=True, owner_resolver=owner)
    store_b = DatabaseTaskStore(engine_b, create_table=True, owner_resolver=owner)
    context = ServerCallContext(tenant="tenant-a")
    task_id = str(uuid4())
    task = new_task(task_id, str(uuid4()), TaskState.TASK_STATE_SUBMITTED)
    try:
        await store_a.save(task, context)
        loaded = await store_b.get(task_id, context)
        assert loaded is not None
        assert loaded.id == task_id
        assert await store_b.get(task_id, ServerCallContext(tenant="tenant-b")) is None
    finally:
        await engine_a.dispose()
        await engine_b.dispose()


def _mcp_boundary_app() -> FastAPI:
    app = FastAPI()

    @app.get("/tool/{tool_name}")
    async def call_tool(tool_name: str) -> dict[str, object]:
        try:
            authorize_tool_execution(tool_name)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        context = get_tool_execution_context()
        return {
            "owner_id": context.owner_id if context else None,
            "run_id": context.run_id if context else None,
            "allowed_tools": list(context.allowed_tools) if context else [],
        }

    return app


@pytest.mark.asyncio
async def test_mcp_asgi_boundary_auth_identity_allow_and_deny() -> None:
    settings = get_settings()
    with (
        patch.object(settings, "require_authentication", True),
        patch.object(settings, "s2s_internal_token", "integration-s2s-token"),
    ):
        app = secure_mcp_app(_mcp_boundary_app())
        transport = httpx.ASGITransport(app=app)
        base_headers = {
            "X-Internal-Service-Token": "integration-s2s-token",
            "X-Agent-Name": "coding_agent",
            "X-Step-ID": "step-mcp",
            "X-Owner-ID": "tenant-a",
            "X-Run-ID": "run-mcp",
        }
        async with httpx.AsyncClient(transport=transport, base_url="http://mcp") as client:
            assert (await client.get("/tool/repo_read_file")).status_code == 401
            no_identity = await client.get(
                "/tool/repo_read_file",
                headers={"X-Internal-Service-Token": "integration-s2s-token"},
            )
            assert no_identity.status_code == 400

            invalid_headers = {**base_headers, "X-Approval-Capability": "invalid"}
            assert (
                await client.get("/tool/repo_read_file", headers=invalid_headers)
            ).status_code == 403

            read_capability = issue_tool_capability(
                run_id="run-mcp",
                step_id="step-mcp",
                owner_id="tenant-a",
                agent_name="coding_agent",
                allowed_tools=[],
            )
            read_response = await client.get(
                "/tool/repo_read_file",
                headers={**base_headers, "X-Approval-Capability": read_capability},
            )
            assert read_response.status_code == 200
            assert read_response.json()["owner_id"] == "tenant-a"

            denied = await client.get(
                "/tool/repo_write_file",
                headers={**base_headers, "X-Approval-Capability": read_capability},
            )
            assert denied.status_code == 403

            write_capability = issue_tool_capability(
                run_id="run-mcp",
                step_id="step-mcp",
                owner_id="tenant-a",
                agent_name="coding_agent",
                allowed_tools=["repo_write_file"],
            )
            allowed = await client.get(
                "/tool/repo_write_file",
                headers={**base_headers, "X-Approval-Capability": write_capability},
            )
            assert allowed.status_code == 200
            assert allowed.json()["allowed_tools"] == ["repo_write_file"]
