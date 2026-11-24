"""FastMCP / MCPServer for Web Search (Port 9003)."""

import argparse
import html
import re

import httpx

try:
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:
    from mcp.server.fastmcp import FastMCP  # type: ignore

mcp = FastMCP("web_search_server", instructions="Search the web for up-to-date information.")


class WebSearchUnavailableError(RuntimeError):
    """Raised when the configured external search provider cannot return real results."""


async def search_web_core(query: str, max_results: int = 5) -> str:
    """Search DuckDuckGo HTML and fail closed when no verifiable result is available."""
    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("Search query must not be empty.")
    if not 1 <= max_results <= 10:
        raise ValueError("max_results must be between 1 and 10.")
    try:
        url = "https://html.duckduckgo.com/html/"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.post(url, data={"q": normalized_query}, headers=headers)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise WebSearchUnavailableError("External web search provider is unavailable.") from exc

    snippets = re.findall(
        r'<a class="result__snippet[^>]*>(.*?)</a>', response.text, flags=re.IGNORECASE
    )
    results = [
        html.unescape(re.sub(r"<[^>]+>", "", snippet)).strip() for snippet in snippets[:max_results]
    ]
    results = [result for result in results if result]
    if not results:
        raise WebSearchUnavailableError("External web search returned no parseable results.")
    numbered = "\n".join(f"{index}. {result}" for index, result in enumerate(results, 1))
    return f"Search results for '{normalized_query}':\n{numbered}"


@mcp.tool()
async def web_search(query: str, max_results: int = 5) -> str:
    """Search external web information.

    Args:
        query: Search keywords or query phrase.
        max_results: Max number of results to retrieve.
    """
    from src.agentic.tools.servers.security import authorize_tool_execution

    authorize_tool_execution("web_search")
    return await search_web_core(query, max_results)


def main() -> None:
    parser = argparse.ArgumentParser(description="Web Search MCP Server")
    parser.add_argument("--port", type=int, default=9003, help="Port to listen on")
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
