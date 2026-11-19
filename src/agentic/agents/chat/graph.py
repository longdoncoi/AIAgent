"""ChatAgent LangGraph definition."""

from typing import Any

from src.agentic.agents.base import build_react_graph
from src.agentic.core.config import get_settings
from src.agentic.llm.gateway import get_llm
from src.agentic.tools.mcp_client import get_mcp_client_manager

_chat_graph_instance: Any = None


def get_chat_graph() -> Any:
    """Builds or returns singleton compiled ChatAgent graph."""
    global _chat_graph_instance
    if _chat_graph_instance is not None:
        return _chat_graph_instance

    settings = get_settings()
    llm = get_llm(alias="fast-llm", temperature=0.3)
    tools = get_mcp_client_manager().get_tools_for_agent("chat_agent")
    prompt_path = settings.prompts_dir / "chat_agent.md"

    _chat_graph_instance = build_react_graph(
        model=llm,
        tools=tools,
        prompt=prompt_path,
    )
    return _chat_graph_instance
