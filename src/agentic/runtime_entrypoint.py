"""Fail-closed process launcher for shell-less production containers."""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Sequence
from types import ModuleType
from typing import NoReturn

_AGENT_MODULES = frozenset(
    {
        "src.agentic.agents.chat.server",
        "src.agentic.agents.coding.server",
        "src.agentic.agents.rag.server",
    }
)
_MCP_MODULES = frozenset(
    {
        "src.agentic.tools.servers.code_exec_server",
        "src.agentic.tools.servers.repo_server",
        "src.agentic.tools.servers.web_search_server",
    }
)


_TEMPORAL_MODULES = frozenset({"src.agentic.workflows.worker"})


def _abort(message: str) -> NoReturn:
    raise SystemExit(message)


def _validated_port(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        port = int(raw)
    except ValueError:
        _abort(f"{name} must be an integer")
    if not 1 <= port <= 65535:
        _abort(f"{name} must be between 1 and 65535")
    return port


def _load_main(module_name: str, allowed: frozenset[str]) -> ModuleType:
    if module_name not in allowed:
        _abort(f"runtime module is not allowlisted: {module_name}")
    module = importlib.import_module(module_name)
    if not callable(getattr(module, "main", None)):
        _abort(f"runtime module has no callable main(): {module_name}")
    return module


def main(argv: Sequence[str] | None = None) -> None:
    """Launch an allowlisted Agent, MCP, or Temporal worker without invoking a shell."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in {"agent", "mcp", "temporal"}:
        _abort("usage: python -m src.agentic.runtime_entrypoint {agent|mcp|temporal}")

    if args[0] == "agent":
        module_name = os.environ.get("AGENT_MODULE", "src.agentic.agents.chat.server")
        port = _validated_port("AGENT_PORT", 9100)
        module = _load_main(module_name, _AGENT_MODULES)
        sys.argv = [module_name, "--host", "0.0.0.0", "--port", str(port)]
    elif args[0] == "mcp":
        module_name = os.environ.get(
            "MCP_SERVER_MODULE", "src.agentic.tools.servers.code_exec_server"
        )
        port = _validated_port("MCP_PORT", 9001)
        transport = os.environ.get("MCP_TRANSPORT", "streamable-http").strip().lower()
        if transport not in {"streamable-http", "sse"}:
            _abort(f"MCP_TRANSPORT must be 'streamable-http' or 'sse', got: {transport}")
        module = _load_main(module_name, _MCP_MODULES)
        sys.argv = [module_name, "--port", str(port), "--transport", transport]
    else:
        module_name = os.environ.get("TEMPORAL_WORKER_MODULE", "src.agentic.workflows.worker")
        module = _load_main(module_name, _TEMPORAL_MODULES)
        sys.argv = [module_name]

    module.main()


if __name__ == "__main__":
    main()
