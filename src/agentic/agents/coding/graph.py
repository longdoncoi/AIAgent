"""CodingAgent LangGraph definition."""

from typing import Any

from src.agentic.agents.base import build_react_graph
from src.agentic.core.config import get_settings
from src.agentic.llm.gateway import get_llm
from src.agentic.tools.mcp_client import get_mcp_client_manager

_coding_graph_instance: Any = None


def get_coding_graph() -> Any:
    """Builds or returns singleton compiled CodingAgent graph."""
    global _coding_graph_instance
    if _coding_graph_instance is not None:
        return _coding_graph_instance

    settings = get_settings()
    llm = get_llm(alias="coding-llm", temperature=0.1)
    tools = get_mcp_client_manager().get_tools_for_agent("coding_agent")
    prompt_path = settings.prompts_dir / "coding_agent.md"

    _coding_graph_instance = build_react_graph(
        model=llm,
        tools=tools,
        prompt=prompt_path,
    )
    return _coding_graph_instance
