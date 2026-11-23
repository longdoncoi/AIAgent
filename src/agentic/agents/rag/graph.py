"""RAGAgent LangGraph definition."""

import threading
from typing import Any

from src.agentic.agents.base import build_react_graph
from src.agentic.core.config import get_settings
from src.agentic.llm.gateway import get_llm
from src.agentic.rag.tools import get_rag_tool

_rag_graph_instance: Any = None
_graph_lock = threading.Lock()


def get_rag_graph() -> Any:
    """Builds or returns singleton compiled RAGAgent graph (thread-safe)."""
    global _rag_graph_instance
    if _rag_graph_instance is not None:
        return _rag_graph_instance
    with _graph_lock:
        if _rag_graph_instance is None:
            settings = get_settings()
            llm = get_llm(alias="reasoning-llm", temperature=0.1, streaming=True)
            tools = [get_rag_tool()]
            prompt_path = settings.prompts_dir / "rag_agent.md"
            _rag_graph_instance = build_react_graph(
                model=llm,
                tools=tools,
                prompt=prompt_path,
            )
    return _rag_graph_instance
