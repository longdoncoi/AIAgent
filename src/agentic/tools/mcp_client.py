"""MCP Client Manager converting FastMCP / local tool handlers into LangChain tools."""

import logging
from functools import lru_cache
from typing import Any, Callable, Coroutine

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from src.agentic.core.config import get_settings
from src.agentic.observability.metrics import TOOL_EXECUTION_TOTAL
from src.agentic.observability.tracing import get_tracer
from src.agentic.safety.guardrails import get_guardrails
from src.agentic.safety.policy import get_policy_manager
from src.agentic.tools.servers.code_exec_server import execute_bash_command, execute_python_code
from src.agentic.tools.servers.repo_server import (
    list_files_core,
    read_file_core,
    search_symbol_core,
    write_file_core,
)
from src.agentic.tools.servers.web_search_server import search_web_core

logger = logging.getLogger(__name__)


# Schema definitions for LangChain StructuredTool bindings
class WebSearchInput(BaseModel):
    query: str = Field(description="Search keywords or query phrase")
    max_results: int = Field(default=5, description="Number of results to return")


class RepoListFilesInput(BaseModel):
    directory: str = Field(default=".", description="Target folder path")
    max_depth: int = Field(default=3, description="Depth limit for directory traversal")


class RepoReadFileInput(BaseModel):
    file_path: str = Field(description="Relative path of the file to read")
    start_line: int = Field(default=1, description="First line index (1-based)")
    end_line: int = Field(default=200, description="Last line index")


class RepoSearchSymbolInput(BaseModel):
    query: str = Field(description="Symbol name or regex pattern to search for")
    directory: str = Field(default=".", description="Target directory")


class RepoWriteFileInput(BaseModel):
    file_path: str = Field(description="Path to target file")
    content: str = Field(description="Full text content to write into file")


class CodeExecPythonInput(BaseModel):
    code: str = Field(description="Python source code to execute")
    timeout: int = Field(default=30, description="Max execution time in seconds")


class CodeExecBashInput(BaseModel):
    command: str = Field(description="Bash or shell command string")
    timeout: int = Field(default=30, description="Max execution time in seconds")


class MCPClientManager:
    """Manages MCP tool registry, policy filtering, and LangChain conversions."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.policy_mgr = get_policy_manager()
        self._raw_registry: dict[str, BaseTool] = {}
        self._init_tools()

    def _wrap_async_tool(
        self, name: str, func: Callable[..., Coroutine[Any, Any, str]]
    ) -> Callable:
        """Wraps an async function with tracing and Prometheus metrics."""

        async def wrapped(*args: Any, **kwargs: Any) -> str:
            tracer = get_tracer()
            with tracer.start_as_current_span(f"mcp.tool.{name}"):
                try:
                    # Tool guardrails validation
                    get_guardrails().validate_tool_input(name, kwargs)
                    result = await func(*args, **kwargs)
                    TOOL_EXECUTION_TOTAL.labels(tool_name=name, status="success").inc()
                    return result
                except Exception as ex:
                    TOOL_EXECUTION_TOTAL.labels(tool_name=name, status="error").inc()
                    logger.error(f"Error executing tool {name}: {ex}")
                    return f"Tool execution error: {str(ex)}"

        return wrapped

    def _init_tools(self) -> None:
        """Initializes LangChain StructuredTool instances from MCP tool definitions."""
        self._raw_registry["web_search"] = StructuredTool(
            name="web_search",
            description="Search the external web for current facts, articles, and documentation.",
            coroutine=self._wrap_async_tool("web_search", search_web_core),
            args_schema=WebSearchInput,
        )

        self._raw_registry["repo_list_files"] = StructuredTool(
            name="repo_list_files",
            description="List the directory hierarchy and files in the repository up to a given depth.",
            func=list_files_core,
            args_schema=RepoListFilesInput,
        )

        self._raw_registry["repo_read_file"] = StructuredTool(
            name="repo_read_file",
            description="Read specific lines from a file in the workspace.",
            func=read_file_core,
            args_schema=RepoReadFileInput,
        )

        self._raw_registry["repo_search_symbol"] = StructuredTool(
            name="repo_search_symbol",
            description="Search for a symbol, function name, or text pattern across files in the codebase.",
            func=search_symbol_core,
            args_schema=RepoSearchSymbolInput,
        )

        self._raw_registry["repo_write_file"] = StructuredTool(
            name="repo_write_file",
            description="Create or overwrite a file with new content. Note: High-risk action requiring HITL.",
            func=write_file_core,
            args_schema=RepoWriteFileInput,
        )

        self._raw_registry["code_exec_python"] = StructuredTool(
            name="code_exec_python",
            description="Execute Python scripts in a sandboxed subprocess and return stdout/stderr.",
            coroutine=self._wrap_async_tool("code_exec_python", execute_python_code),
            args_schema=CodeExecPythonInput,
        )

        self._raw_registry["code_exec_bash"] = StructuredTool(
            name="code_exec_bash",
            description="Execute shell commands in a sandbox. Note: High-risk action requiring HITL.",
            coroutine=self._wrap_async_tool("code_exec_bash", execute_bash_command),
            args_schema=CodeExecBashInput,
        )

    def get_tool(self, tool_name: str) -> BaseTool | None:
        """Retrieves a specific tool by name."""
        return self._raw_registry.get(tool_name)

    def get_tools_for_agent(self, agent_name: str) -> list[BaseTool]:
        """Returns the list of tools permitted for an agent according to policy allowlist."""
        allowed_names = self.policy_mgr.get_agent_tools(agent_name)
        tools = []
        for name in allowed_names:
            if name in self._raw_registry:
                tools.append(self._raw_registry[name])
        return tools


@lru_cache
def get_mcp_client_manager() -> MCPClientManager:
    """Singleton getter for MCPClientManager."""
    return MCPClientManager()
