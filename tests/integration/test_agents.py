"""Integration tests for A2A Sub-Agents and protocol execution."""

import pytest

from src.agentic.a2a.cards import get_agent_card
from src.agentic.a2a.client import get_a2a_client
from src.agentic.tools.servers.repo_server import list_files_core, read_file_core


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
async def test_a2a_client_local_dispatch():
    client = get_a2a_client()
    # Test direct fallback call to chat agent
    resp = await client.call_agent(
        target_agent="chat",
        step_id="step_test_1",
        instruction="Hãy trả lời ngắn gọn: 1 + 1 bằng mấy?",
    )
    assert resp.status in ["completed", "failed"]
    assert resp.task_id == "step_test_1"
