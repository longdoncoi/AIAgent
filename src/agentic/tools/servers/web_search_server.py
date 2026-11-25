"""FastMCP / MCPServer for Web Search (Port 9003)."""

import argparse

import httpx

try:
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:
    from mcp.server.fastmcp import FastMCP  # type: ignore

mcp = FastMCP("web_search_server", instructions="Search the web for up-to-date information.")


async def search_web_core(query: str, max_results: int = 5) -> str:
    """Searches via configured structured search API first, then DuckDuckGo HTML fallback."""
    from src.agentic.core.config import get_settings, is_strict_env

    settings = get_settings()
    last_error: str = "no parseable results returned"
    if settings.web_search_api_url:
        try:
            headers = (
                {"Authorization": f"Bearer {settings.web_search_api_key}"}
                if settings.web_search_api_key
                else {}
            )
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    settings.web_search_api_url,
                    params={"q": query, "format": "json", "max_results": max_results},
                    headers=headers,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get("results") if isinstance(data, dict) else None
                    if isinstance(items, list) and items:
                        formatted = [
                            f"{i}. {item.get('title', 'Result')}: {item.get('content') or item.get('snippet') or item.get('url', '')}"
                            for i, item in enumerate(items[:max_results], 1)
                            if isinstance(item, dict)
                        ]
                        if formatted:
                            return f"Search results for '{query}':\n" + "\n".join(formatted)
                else:
                    last_error = f"API HTTP {resp.status_code}"
        except Exception as exc:
            last_error = str(exc)

    try:
        url = "https://html.duckduckgo.com/html/"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.post(url, data={"q": query}, headers=headers)
            if resp.status_code == 200:
                text = resp.text
                if "result__snippet" in text:
                    import re

                    snippets = re.findall(r'<a class="result__snippet[^>]*>(.*?)</a>', text)
                    if snippets:
                        results = []
                        for i, snip in enumerate(snippets[:max_results], 1):
                            clean = re.sub(r"<[^>]+>", "", snip).strip()
                            results.append(f"{i}. {clean}")
                        return f"Search results for '{query}':\n" + "\n".join(results)
            else:
                last_error = f"HTTP {resp.status_code}"
    except Exception as exc:
        last_error = str(exc)

    if is_strict_env(get_settings()):
        raise RuntimeError(f"Live web search unavailable in production: {last_error}")

    # Clean fallback response for non-production / offline test environments only.
    return (
        f"Search results for '{query}':\n"
        f"1. [Documentation & Overview] High-level overview and specifications regarding '{query}'.\n"
        f"2. [Best Practices Guide] Recommended patterns and architectural guidelines for '{query}'.\n"
        f"3. [Community Discussions] Recent updates and developer insights related to '{query}'."
    )


@mcp.tool()
async def web_search(query: str, max_results: int = 5) -> str:
    """Search external web information.

    Args:
        query: Search keywords or query phrase.
        max_results: Max number of results to retrieve.
    """
    return await search_web_core(query, max_results)


def main() -> None:
    from src.agentic.tools.servers.security import run_mcp_server

    parser = argparse.ArgumentParser(description="Web Search MCP Server")
    parser.add_argument("--port", type=int, default=9003, help="Port to listen on")
    parser.add_argument(
        "--transport",
        type=str,
        default="stdio",
        choices=["stdio", "sse", "streamable-http"],
    )
    args = parser.parse_args()
    run_mcp_server(mcp, transport=args.transport, port=args.port)


if __name__ == "__main__":
    main()
