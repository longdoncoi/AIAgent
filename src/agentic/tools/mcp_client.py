"""Policy-enforcing MCP client bindings for LangChain agents."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from src.agentic.core.config import get_settings
from src.agentic.observability.metrics import TOOL_EXECUTION_TOTAL
from src.agentic.observability.tracing import get_tracer
from src.agentic.safety.approvals import verify_approval_grant
from src.agentic.safety.guardrails import get_guardrails
from src.agentic.safety.policy import get_policy_manager
from src.agentic.tools.execution_context import get_tool_execution_context
from src.agentic.tools.servers.code_exec_server import execute_bash_command, execute_python_code
from src.agentic.tools.servers.repo_server import (
    list_files_core,
    read_file_core,
    search_symbol_core,
    write_file_core,
)
from src.agentic.tools.servers.web_search_server import search_web_core

logger = logging.getLogger(__name__)


class WebSearchInput(BaseModel):
    query: str
    max_results: int = Field(default=5, ge=1, le=10)


class RepoListFilesInput(BaseModel):
    directory: str = "."
    max_depth: int = Field(default=3, ge=1, le=8)


class RepoReadFileInput(BaseModel):
    file_path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=200, ge=1, le=5000)


class RepoSearchSymbolInput(BaseModel):
    query: str
    directory: str = "."


class RepoWriteFileInput(BaseModel):
    file_path: str
    content: str


class CodeExecPythonInput(BaseModel):
    code: str
    timeout: int = Field(default=30, ge=1, le=60)


class CodeExecBashInput(BaseModel):
    command: str
    timeout: int = Field(default=30, ge=1, le=60)


AsyncTool = Callable[..., Awaitable[str]]


class MCPClientManager:
    """Builds per-agent tools and enforces policy at the execution boundary."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.policy_mgr = get_policy_manager()
        self._schemas: dict[str, type[BaseModel]] = {
            "web_search": WebSearchInput,
            "repo_list_files": RepoListFilesInput,
            "repo_read_file": RepoReadFileInput,
            "repo_search_symbol": RepoSearchSymbolInput,
            "repo_write_file": RepoWriteFileInput,
            "code_exec_python": CodeExecPythonInput,
            "code_exec_bash": CodeExecBashInput,
        }
        self._descriptions = {
            "web_search": "Search the external web for current facts and documentation.",
            "repo_list_files": "List files in the repository.",
            "repo_read_file": "Read selected lines from a repository file.",
            "repo_search_symbol": "Search repository text and symbols.",
            "repo_write_file": "Create or overwrite a repository file; requires approval.",
            "code_exec_python": "Execute Python in an isolated sandbox.",
            "code_exec_bash": "Execute a shell command in an isolated sandbox; requires approval.",
        }
        self._local: dict[str, AsyncTool] = {
            "web_search": search_web_core,
            "repo_list_files": self._asyncify(list_files_core),
            "repo_read_file": self._asyncify(read_file_core),
            "repo_search_symbol": self._asyncify(search_symbol_core),
            "repo_write_file": self._asyncify(write_file_core),
            "code_exec_python": execute_python_code,
            "code_exec_bash": execute_bash_command,
        }

    @staticmethod
    def _asyncify(func: Callable[..., str]) -> AsyncTool:
        async def invoke(**kwargs: Any) -> str:
            return await asyncio.to_thread(func, **kwargs)

        return invoke

    def _wrap_async_tool(self, name: str, func: AsyncTool) -> AsyncTool:
        """Backward-compatible instrumentation helper; registry tools use `_invoke`."""
        async def wrapped(**kwargs: Any) -> str:
            try:
                get_guardrails().validate_tool_input(name, kwargs)
                result = await func(**kwargs)
                TOOL_EXECUTION_TOTAL.labels(tool_name=name, status="success").inc()
                return result
            except Exception as exc:
                TOOL_EXECUTION_TOTAL.labels(tool_name=name, status="error").inc()
                logger.exception("Tool execution failed: %s", name)
                return f"Tool execution error: {exc}"

        return wrapped

    def _server_url(self, tool_name: str) -> str:
        if tool_name.startswith("repo_"):
            return self.settings.mcp_repo_url
        if tool_name.startswith("code_exec_"):
            return self.settings.mcp_code_exec_url
        return self.settings.mcp_web_search_url

    async def _remote_call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        headers = {"X-Internal-Service-Token": self.settings.s2s_internal_token}
        async with sse_client(
            self._server_url(tool_name), headers=headers, timeout=5.0, sse_read_timeout=65.0
        ) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments=arguments)
        if getattr(result, "isError", False):
            raise RuntimeError("Remote MCP tool reported an execution error.")
        parts = [str(item.text) for item in getattr(result, "content", []) if hasattr(item, "text")]
        return "\n".join(parts)

    async def _invoke(self, bound_agent: str, tool_name: str, **kwargs: Any) -> str:
        context = get_tool_execution_context()
        effective_agent = context.agent_name if context is not None else bound_agent
        policy = get_policy_manager()
        if not policy.is_tool_allowed(effective_agent, tool_name):
            raise PermissionError(f"Tool '{tool_name}' is not allowed for this agent.")

        # Approval-gated tools require a *server-issued* signed grant. A boolean
        # propagated from the caller (A2A metadata) is never sufficient.
        if policy.is_approval_required(tool_name):
            grant = (
                verify_approval_grant(
                    context.approval_token,
                    step_id=context.step_id,
                    fingerprint=context.step_fingerprint or "",
                    owner_id=context.owner_id,
                    agent_name=effective_agent,
                    tool_name=tool_name,
                    arguments=kwargs,
                )
                if context is not None
                else None
            )
            if grant is None:
                TOOL_EXECUTION_TOTAL.labels(tool_name=tool_name, status="denied").inc()
                logger.warning(
                    "Denied approval-gated tool '%s' for step '%s': no valid signed HITL grant",
                    tool_name,
                    context.step_id if context is not None else "<none>",
                )
                raise PermissionError(f"Tool '{tool_name}' requires step-scoped HITL approval.")

        tracer = get_tracer()
        with tracer.start_as_current_span(f"mcp.tool.{tool_name}"):
            try:
                get_guardrails().validate_tool_input(tool_name, kwargs)
                if self.settings.use_remote_mcp:
                    result = await self._remote_call(tool_name, kwargs)
                else:
                    result = await self._local[tool_name](**kwargs)
                TOOL_EXECUTION_TOTAL.labels(tool_name=tool_name, status="success").inc()
                return result
            except Exception:
                TOOL_EXECUTION_TOTAL.labels(tool_name=tool_name, status="error").inc()
                logger.exception("Tool execution failed: %s", tool_name)
                raise

    def _build_tool(self, agent_name: str, tool_name: str) -> BaseTool:
        async def coroutine(**kwargs: Any) -> str:
            return await self._invoke(agent_name, tool_name, **kwargs)

        return StructuredTool(
            name=tool_name,
            description=self._descriptions[tool_name],
            coroutine=coroutine,
            args_schema=self._schemas[tool_name],
        )

    def get_tool(self, tool_name: str, agent_name: str = "coding_agent") -> BaseTool | None:
        """Build a tool only if the target agent's policy allowlist permits it."""
        if tool_name not in self._schemas:
            return None
        if not self.policy_mgr.is_tool_allowed(agent_name, tool_name):
            logger.warning("Tool '%s' is not allowed for agent '%s'", tool_name, agent_name)
            return None
        return self._build_tool(agent_name, tool_name)

    def get_tools_for_agent(self, agent_name: str) -> list[BaseTool]:
        return [
            self._build_tool(agent_name, name)
            for name in self.policy_mgr.get_agent_tools(agent_name)
            if name in self._schemas
        ]


@lru_cache
def get_mcp_client_manager() -> MCPClientManager:
    return MCPClientManager()


def reload_mcp_client_manager() -> MCPClientManager:
    get_mcp_client_manager.cache_clear()
    return get_mcp_client_manager()
