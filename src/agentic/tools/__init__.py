"""Model Context Protocol (MCP) tool integration layer."""

from src.agentic.tools.mcp_client import MCPClientManager, get_mcp_client_manager

__all__ = ["MCPClientManager", "get_mcp_client_manager"]
