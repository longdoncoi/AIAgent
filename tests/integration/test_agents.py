"""Integration tests for A2A Sub-Agents and protocol execution."""

import httpx
import pytest
from langchain_core.messages import AIMessage

from src.agentic.a2a.cards import get_agent_card
from src.agentic.a2a.client import get_a2a_client
from src.agentic.a2a.executor import create_a2a_server
from src.agentic.core.config import get_settings
from src.agentic.tools.servers.repo_server import list_files_core, read_file_core


class _DeterministicChatGraph:
    async def ainvoke(self, state, config=None):
        msg = state["messages"][0].content if state.get("messages") else ""
        return {"messages": [AIMessage(content=f"2 (echo: {msg[:20]})")]}


def test_agent_cards_registry():
    chat_card = get_agent_card("chat_agent")
    assert chat_card is not None
    assert chat_card.name == "chat_agent"
    assert "general_chit_chat" in chat_card.skills

    rag_card = get_agent_card("rag")
    assert rag_card is not None
    assert "policy_lookup" in rag_card.skills

    coding_card = get_agent_card("coding_agent")
    assert coding_card is not None
    assert "code_generation" in coding_card.skills


def test_mcp_repo_tools():
    # Test file listing
    listing = list_files_core(".", max_depth=1)
    assert "pyproject.toml" in listing or "[FILE]" in listing

    # Test file reading
    content = read_file_core("pyproject.toml", start_line=1, end_line=5)
    assert "project" in content


@pytest.mark.asyncio
async def test_a2a_client_local_dispatch(monkeypatch: pytest.MonkeyPatch):
    client = get_a2a_client()
    monkeypatch.setitem(client._local_executors, "chat", _DeterministicChatGraph())
    resp = await client.call_agent(
        target_agent="chat",
        step_id="step_test_1",
        instruction="Hãy trả lời ngắn gọn: 1 + 1 bằng mấy?",
    )
    assert resp.status == "completed"
    assert resp.task_id == "step_test_1"
    assert "2" in resp.output


@pytest.mark.asyncio
async def test_a2a_http_server_roundtrip():
    card = get_agent_card("chat_agent")
    assert card is not None
    app = create_a2a_server(card, _DeterministicChatGraph())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://agentic-chat-svc:9100",
    ) as http:
        health = await http.get("/health")
        assert health.status_code == 200

        settings = get_settings()
        res = await http.post(
            "/a2a/tasks",
            headers={"X-Internal-Service-Token": settings.s2s_internal_token},
            json={
                "task_id": "step_http_1",
                "caller": "supervisor",
                "target_agent": "chat_agent",
                "payload": {"instruction": "1 + 1?"},
                "metadata": {"tenant_id": "default"},
            },
        )
        assert res.status_code == 200
        payload = res.json()
        assert payload["task_id"] == "step_http_1"
        assert payload["status"] == "completed"
        assert "2" in payload["output"]
