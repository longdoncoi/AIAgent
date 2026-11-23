"""FastMCP / MCPServer for Repository and File Operations (Port 9002)."""

import argparse
import asyncio
import os
import re
from pathlib import Path

try:
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:
    from mcp.server.fastmcp import FastMCP  # type: ignore

mcp = FastMCP(
    "repo_server",
    instructions="Provides tools to inspect, read, search, and edit files in the repository.",
)

IGNORED_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    ".ruff_cache",
    "dist",
    "build",
}

# Protected paths that coding agents must never overwrite to prevent security policy tampering
DENIED_WRITE_PATTERNS = {
    "configs",
    "src",
    ".git",
    "migrations",
    ".env",
    "deploy",
    "alembic.ini",
}


def _get_workspace_root() -> Path:
    """Retrieves the canonical root directory of the workspace."""
    custom_root = os.environ.get("WORKSPACE_ROOT")
    if custom_root:
        return Path(custom_root).resolve()
    try:
        from src.agentic.core.config import get_settings

        return get_settings().project_root.resolve()
    except Exception:
        return Path.cwd().resolve()


def _validate_safe_path(target_path: str, is_write: bool = False) -> Path:
    """Validates that a path stays strictly within workspace boundary (prevents Path Traversal)
    and enforces strict access control policies against system configuration tampering.
    """
    root = _get_workspace_root()
    path = Path(target_path)
    if not path.is_absolute():
        path = (root / path).resolve()
    else:
        path = path.resolve()

    if not path.is_relative_to(root):
        raise PermissionError(
            f"Security Violation: Target path '{target_path}' escapes workspace boundary ('{root}')."
        )

    rel_posix = path.relative_to(root).as_posix()
    top_dir = rel_posix.split("/")[0] if "/" in rel_posix else rel_posix

    if is_write:
        for denied in DENIED_WRITE_PATTERNS:
            if rel_posix == denied or rel_posix.startswith(f"{denied}/") or top_dir == denied:
                raise PermissionError(
                    f"Security Violation: Writing to protected system directory/file '{rel_posix}' is forbidden."
                )

    # Protect secret files from direct read
    if rel_posix == ".env" or rel_posix.startswith(".env.") or rel_posix.startswith("data/memory/checkpoints"):
        raise PermissionError(
            f"Security Violation: Direct access to secret file '{rel_posix}' is forbidden."
        )

    return path


def list_files_core(directory: str = ".", max_depth: int = 3) -> str:
    """Lists repository directory tree up to max_depth."""
    try:
        base_path = _validate_safe_path(directory)
    except PermissionError as pe:
        return str(pe)

    if not base_path.exists():
        return f"Directory '{directory}' does not exist."

    results: list[str] = []

    def _walk(current: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            for item in sorted(current.iterdir()):
                if item.name in IGNORED_DIRS:
                    continue
                rel_path = item.relative_to(base_path)
                prefix = "  " * (depth - 1)
                if item.is_dir():
                    results.append(f"{prefix}[DIR]  {rel_path.as_posix()}/")
                    _walk(item, depth + 1)
                else:
                    results.append(f"{prefix}[FILE] {rel_path.as_posix()}")
        except PermissionError:
            results.append(f"{'  ' * depth}[PERMISSION DENIED]")

    _walk(base_path, 1)
    return "\n".join(results) if results else "Directory is empty."


def read_file_core(file_path: str, start_line: int = 1, end_line: int = 200) -> str:
    """Reads lines from a file with line numbers."""
    try:
        path = _validate_safe_path(file_path)
    except PermissionError as pe:
        return str(pe)

    if not path.is_file():
        return f"File '{file_path}' not found."

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            total_lines = len(lines)
            selected = lines[max(0, start_line - 1) : min(total_lines, end_line)]
            numbered = [f"{start_line + i:4d} | {line.rstrip()}" for i, line in enumerate(selected)]
            header = f"File: {path.name} (Lines {start_line}-{min(total_lines, end_line)} of {total_lines})\n"
            return header + "\n".join(numbered)
    except Exception as e:
        return f"Error reading file '{file_path}': {e}"


_NESTED_QUANTIFIER = re.compile(r"\([^)]*[+*][^)]*\)[+*{]")
_MAX_MATCH_LINE_CHARS = 4000


def search_symbol_core(query: str, directory: str = ".") -> str:
    """Searches for regex/symbol across text files in directory with ReDoS protection."""
    if len(query) > 200:
        return "Search query rejected: exceeds maximum length of 200 characters."
    # Catastrophic-backtracking constructs (e.g. ``(a+)+$``) can hang the whole
    # MCP server. Reject them up front instead of relying on a length check.
    if _NESTED_QUANTIFIER.search(query) or query.count("|") > 32:
        return "Search query rejected: potentially catastrophic regular expression."

    try:
        base_path = _validate_safe_path(directory)
    except PermissionError as pe:
        return str(pe)

    if not base_path.exists():
        return f"Directory '{directory}' not found."

    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error:
        # Fall back to escaped literal pattern if regex is malformed
        pattern = re.compile(re.escape(query), re.IGNORECASE)

    matches: list[str] = []

    for root, dirs, files in os.walk(base_path):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for file in files:
            file_path = Path(root) / file
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line_num, line in enumerate(f, start=1):
                        if len(line) > _MAX_MATCH_LINE_CHARS:
                            continue
                        if pattern.search(line):
                            rel_path = file_path.relative_to(base_path)
                            matches.append(f"{rel_path.as_posix()}:{line_num}: {line.strip()}")
                            if len(matches) >= 50:
                                matches.append("... [Output truncated: match limit reached]")
                                return "\n".join(matches)
            except Exception:
                continue

    return "\n".join(matches) if matches else f"No matches found for '{query}'."


def write_file_core(file_path: str, content: str) -> str:
    """Writes content to a file, creating parent directories if needed, blocking protected paths."""
    try:
        path = _validate_safe_path(file_path, is_write=True)
    except PermissionError as pe:
        return str(pe)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully wrote {len(content)} bytes to '{file_path}'."
    except Exception as e:
        return f"Failed to write to '{file_path}': {e}"


@mcp.tool()
async def repo_list_files(directory: str = ".", max_depth: int = 3) -> str:
    """List directory tree up to max_depth."""
    return await asyncio.to_thread(list_files_core, directory, max_depth)


@mcp.tool()
async def repo_read_file(file_path: str, start_line: int = 1, end_line: int = 200) -> str:
    """Read a slice of lines from a file."""
    return await asyncio.to_thread(read_file_core, file_path, start_line, end_line)


@mcp.tool()
async def repo_search_symbol(query: str, directory: str = ".") -> str:
    """Search for a symbol or text pattern in files."""
    return await asyncio.to_thread(search_symbol_core, query, directory)


@mcp.tool()
async def repo_write_file(file_path: str, content: str) -> str:
    """Write or overwrite a file with given content."""
    return await asyncio.to_thread(write_file_core, file_path, content)


def main() -> None:
    parser = argparse.ArgumentParser(description="Repository MCP Server")
    parser.add_argument("--port", type=int, default=9002, help="Port to listen on")
    parser.add_argument("--transport", type=str, default="stdio", choices=["stdio", "sse"])
    args = parser.parse_args()

    if args.transport == "sse":
        from typing import Any, cast

        import uvicorn

        app = getattr(mcp, "sse_app", None) or getattr(mcp, "streamable_http_app", None)
        if callable(app):
            from src.agentic.tools.servers.security import secure_mcp_app

            uvicorn.run(secure_mcp_app(app()), host="0.0.0.0", port=args.port)
        else:
            from src.agentic.tools.servers.security import secure_mcp_app

            uvicorn.run(secure_mcp_app(cast(Any, mcp)), host="0.0.0.0", port=args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
