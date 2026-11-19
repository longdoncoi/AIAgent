"""RAG tools for policy and documentation retrieval with semantic caching."""

import asyncio
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from src.agentic.cache.rag_cache import get_rag_cache
from src.agentic.rag.reranker import CrossEncoderReranker
from src.agentic.rag.retriever import get_hybrid_retriever


class SearchPolicyInput(BaseModel):
    query: str = Field(description="Policy, rule, or technical concept to query in the enterprise knowledge base")
    filters: dict[str, Any] | None = Field(default=None, description="Optional metadata filters (e.g. category, document name)")


def search_policy(query: str, filters: dict[str, Any] | None = None) -> str:
    """Queries internal documentation using hybrid search, cross-encoder reranking, and cache with stampede protection."""
    cache = get_rag_cache()

    def _do_retrieve() -> str:
        retriever = get_hybrid_retriever()
        reranker = CrossEncoderReranker(top_n=3)

        raw_nodes = retriever.retrieve(query, top_k=6)
        if not raw_nodes:
            return f"No relevant enterprise documentation found for query: '{query}'."

        reranked = reranker.rerank(query, raw_nodes)

        results: list[str] = []
        for i, item in enumerate(reranked, 1):
            source = item.node.metadata.get("file_name", "internal_doc")
            score = f"{item.score:.3f}" if item.score is not None else "N/A"
            node_text = item.node.get_content() if hasattr(item.node, "get_content") else getattr(item.node, "text", "")
            results.append(f"--- Document Source [{i}]: {source} (Score: {score}) ---\n{node_text.strip()}")

        return "\n\n".join(results)

    domain = (filters.get("domain") or filters.get("category") or "general") if filters else "general"
    return cache.get_or_compute(query, _do_retrieve, top_k=3, domain=str(domain))


async def asearch_policy(query: str, filters: dict[str, Any] | None = None) -> str:
    """Asynchronously queries internal documentation, offloading CPU / disk retrieval to worker thread."""
    return await asyncio.to_thread(search_policy, query, filters)


def get_rag_tool() -> StructuredTool:
    """Returns the LangChain StructuredTool for search_policy supporting both sync and non-blocking async execution."""
    return StructuredTool(
        name="search_policy",
        description="Search enterprise policies, internal guidelines, and technical documentation.",
        func=search_policy,
        coroutine=asearch_policy,
        args_schema=SearchPolicyInput,
    )
