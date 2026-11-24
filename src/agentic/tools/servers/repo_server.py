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

# Writes are allowed ONLY in a dedicated scratch root; denylists cannot cover
# every sensitive path (.github/workflows, pyproject, agent instructions...).
WRITABLE_ROOTS = {"data/scratch"}
DENIED_READ_PATTERNS = {
    ".git", ".env", "data/memory", "deploy",
    "configs/litellm_config.yaml", ".context", "evidence", "uv.lock",
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

    if is_write and not any(
        rel_posix == allowed or rel_posix.startswith(f"{allowed}/")
        for allowed in WRITABLE_ROOTS
    ):
        raise PermissionError(
            f"Security Violation: Writing to '{rel_posix}' is forbidden. "
            f"Writes are only allowed under {sorted(WRITABLE_ROOTS)}."
        )

    if any(
        rel_posix == denied or rel_posix.startswith(f"{denied}/")
        for denied in DENIED_READ_PATTERNS
    ):
        raise PermissionError(
            f"Security Violation: Direct access to secret file '{rel_posix}' is forbidden."
        )

    return path


def _validate_discovered_path(path: Path) -> Path:
    """Validate a directory entry before traversal or opening it.

    Validating only the caller-supplied root is insufficient: a symlink or
    junction below that root can resolve to a sensitive path outside the
    workspace.  Resolve every discovered entry and apply the same boundary and
    deny-list policy before using it.
    """
    return _validate_safe_path(str(path))


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
                try:
                    safe_item = _validate_discovered_path(item)
                except (OSError, PermissionError):
                    # Do not disclose the target or descend into an unsafe
                    # symlink/junction.
                    continue
                rel_path = item.relative_to(base_path)
                prefix = "  " * (depth - 1)
                if safe_item.is_dir():
                    results.append(f"{prefix}[DIR]  {rel_path.as_posix()}/")
                    _walk(safe_item, depth + 1)
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

    for root, dirs, files in os.walk(base_path, followlinks=False):
        safe_dirs: list[str] = []
        for directory_name in dirs:
            if directory_name in IGNORED_DIRS:
                continue
            try:
                _validate_discovered_path(Path(root) / directory_name)
            except (OSError, PermissionError):
                continue
            safe_dirs.append(directory_name)
        dirs[:] = safe_dirs
        for file in files:
            file_path = Path(root) / file
            try:
                safe_file = _validate_discovered_path(file_path)
                with open(safe_file, "r", encoding="utf-8", errors="ignore") as f:
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
