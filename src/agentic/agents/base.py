"""Reusable ReAct graph builder for all L3 Sub-Agents."""

import logging
import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.errors import GraphInterrupt

from src.agentic.core.errors import GuardrailViolation, ToolCallApprovalRequired

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=UserWarning, module="langgraph.prebuilt")
    warnings.filterwarnings("ignore", category=DeprecationWarning, module="langgraph.prebuilt")
    from langgraph.prebuilt import ToolNode, create_react_agent

logger = logging.getLogger(__name__)


def _handle_react_tool_error(exc: Exception) -> str:
    """Propagate security/HITL/interrupt control-flow exceptions instead of masking them."""
    if isinstance(
        exc,
        (
            ToolCallApprovalRequired,
            PermissionError,
            GuardrailViolation,
            GraphInterrupt,
        ),
    ):
        raise exc
    return f"Tool execution failed: {exc}"


def load_prompt_file(prompt_path: Path) -> str:
    """Reads prompt template from markdown file."""
    if prompt_path.exists():
        try:
            with open(prompt_path, encoding="utf-8") as f:
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

    tool_list = list(tools)
    tool_node: Any = (
        ToolNode(tool_list, handle_tool_errors=_handle_react_tool_error)
        if tool_list
        else []
    )
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        agent_graph = create_react_agent(
            model=model,
            tools=tool_node,
            prompt=system_prompt,
        )
    return agent_graph
