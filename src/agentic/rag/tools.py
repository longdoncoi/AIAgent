"""RAG tools for policy and documentation retrieval with semantic caching."""

import asyncio
import json
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from src.agentic.cache.rag_cache import get_rag_cache
from src.agentic.core.config import get_settings
from src.agentic.rag.reranker import CrossEncoderReranker
from src.agentic.rag.retriever import get_hybrid_retriever
from src.agentic.tools.execution_context import get_tool_execution_context

# These filter keys carry authorization semantics. They are NEVER accepted
# from the (LLM-controlled) tool argument: an attacker could otherwise pass
# ``{"tenant_id": "victim"}`` and read another tenant's documents.
AUTHORITATIVE_FILTER_KEYS: frozenset[str] = frozenset(
    {"tenant_id", "owner_id", "acl", "acls", "tenant", "owner"}
)

CITATION_MARKER_START = "[CITATIONS]"
CITATION_MARKER_END = "[/CITATIONS]"


class SearchPolicyInput(BaseModel):
    query: str = Field(
        description="Policy, rule, or technical concept to query in the enterprise knowledge base"
    )
    filters: dict[str, Any] | None = Field(
        default=None, description="Optional metadata filters (e.g. category, document name)"
    )


def _authoritative_tenant_id() -> str:
    """Return the verified tenant for this tool call (fail-closed default)."""
    context = get_tool_execution_context()
    if context and context.owner_id:
        return context.owner_id
    # No verified claim: fall back to the trusted, config-provided default.
    # Never derive a tenant from the LLM-supplied ``filters`` argument.
    return str(getattr(get_settings(), "rag_default_tenant_id", "public"))


def _build_effective_filters(filters: dict[str, Any] | None) -> dict[str, Any]:
    """Strip caller-supplied auth keys and pin the verified tenant."""
    effective = {
        key: value
        for key, value in (filters or {}).items()
        if key not in AUTHORITATIVE_FILTER_KEYS
    }
    # Plain assignment (not setdefault): the authoritative value always wins.
    effective["tenant_id"] = _authoritative_tenant_id()
    return effective


def _build_citation(item: Any, index: int) -> dict[str, Any]:
    """Produce one structured citation with stable identifiers."""
    metadata = getattr(item.node, "metadata", {}) or {}
    node_id = str(getattr(item.node, "node_id", "") or "")
    doc_id = str(metadata.get("doc_id") or metadata.get("document_id") or node_id)
    file_name = str(metadata.get("file_name") or metadata.get("source") or "internal_doc")
    source = str(metadata.get("source") or metadata.get("file_path") or file_name)
    citation: dict[str, Any] = {
        "index": index,
        "doc_id": doc_id,
        "file_name": file_name,
        "source": source,
    }
    page = metadata.get("page") or metadata.get("page_number")
    if page is not None:
        citation["page"] = page
    return citation


def _format_citations(citations: list[dict[str, Any]]) -> str:
    """Serialize structured citations into a parseable tool-output block."""
    payload = json.dumps({"citations": citations}, ensure_ascii=False, sort_keys=True)
    return f"{CITATION_MARKER_START}\n{payload}\n{CITATION_MARKER_END}"


def validate_citations(text: str) -> bool:
    """Return True when ``text`` contains at least one well-formed citation.

    The reflector (or any other caller) can use this as a cheap structural
    gate: a response with no citations, a malformed block, or a citation
    missing a document identifier fails validation.
    """
    if not text or CITATION_MARKER_START not in text or CITATION_MARKER_END not in text:
        return False
    try:
        block = text.split(CITATION_MARKER_START, 1)[1].split(CITATION_MARKER_END, 1)[0]
        parsed = json.loads(block.strip())
        if not isinstance(parsed, dict):
            return False
        citations = parsed.get("citations")
        if not isinstance(citations, list) or not citations:
            return False
        for citation in citations:
            if not isinstance(citation, dict):
                continue
            if not str(citation.get("doc_id", "")).strip():
                continue
            if str(citation.get("file_name", "")).strip() or str(
                citation.get("source", "")
            ).strip():
                return True
        return False
    except (ValueError, TypeError, IndexError):
        return False


def search_policy(query: str, filters: dict[str, Any] | None = None) -> str:
    """Queries internal documentation using hybrid search, cross-encoder reranking, and cache.

    Authorization-relevant filter keys supplied by the caller are ignored; the
    tenant always comes from the verified execution context. The returned text
    contains a structured ``[CITATIONS]`` JSON block suitable for validation
    with :func:`validate_citations`.
    """
    cache = get_rag_cache()
    effective_filters = _build_effective_filters(filters)

    def _do_retrieve() -> str:
        retriever = get_hybrid_retriever()
        reranker = CrossEncoderReranker(top_n=3)

        raw_nodes = retriever.retrieve(query, top_k=6, filters=effective_filters)
        if not raw_nodes:
            return f"No relevant enterprise documentation found for query: '{query}'."

        reranked = reranker.rerank(query, raw_nodes)

        results: list[str] = []
        citations: list[dict[str, Any]] = []
        for i, item in enumerate(reranked, 1):
            metadata = getattr(item.node, "metadata", {}) or {}
            source = str(metadata.get("file_name", "internal_doc"))
            score = f"{item.score:.3f}" if item.score is not None else "N/A"
            node_text = (
                item.node.get_content()
                if hasattr(item.node, "get_content")
                else getattr(item.node, "text", "")
            )
            citations.append(_build_citation(item, i))
            results.append(
                f"--- Document Source [{i}]: {source} (Score: {score}) ---\n{node_text.strip()}"
            )

        return "\n\n".join(results) + "\n\n" + _format_citations(citations)

    domain = (
        (effective_filters.get("domain") or effective_filters.get("category") or "general")
        if effective_filters
        else "general"
    )
    # Scope is derived from the effective (authoritative) filters only; a
    # spoofed tenant can never create or select a cache entry.
    scope = json.dumps(effective_filters, sort_keys=True, ensure_ascii=False, default=str)
    return cache.get_or_compute(
        query, _do_retrieve, top_k=3, domain=str(domain), scope=scope
    )


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
