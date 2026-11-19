"""Reusable ReAct graph builder for all L3 Sub-Agents."""

import logging
import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=UserWarning, module="langgraph.prebuilt")
    warnings.filterwarnings("ignore", category=DeprecationWarning, module="langgraph.prebuilt")
    from langgraph.prebuilt import create_react_agent

logger = logging.getLogger(__name__)


def load_prompt_file(prompt_path: Path) -> str:
    """Reads prompt template from markdown file."""
    if prompt_path.exists():
        try:
            with open(prompt_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception as e:
            logger.warning(f"Could not read prompt from {prompt_path}: {e}")
    return "You are an AI assistant."


def build_react_graph(
    model: BaseChatModel,
    tools: Sequence[BaseTool],
    prompt: str | Path | None = None,
) -> Any:
    """Constructs a compiled LangGraph ReAct agent loop with bound tools and system prompt."""
    system_prompt = ""
    if isinstance(prompt, Path):
        system_prompt = load_prompt_file(prompt)
    elif isinstance(prompt, str):
        system_prompt = prompt

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        agent_graph = create_react_agent(
            model=model,
            tools=list(tools),
            prompt=system_prompt,
        )
    return agent_graph
