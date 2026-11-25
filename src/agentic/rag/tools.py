"""RAG tools for policy and documentation retrieval with semantic caching."""

import asyncio
import json
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from src.agentic.cache.rag_cache import get_rag_cache
from src.agentic.core.config import get_settings, is_strict_env
from src.agentic.core.execution_context import get_tool_execution_context
from src.agentic.rag.reranker import CrossEncoderReranker
from src.agentic.rag.retriever import get_hybrid_retriever

# These filter keys carry authorization semantics. They are NEVER accepted
# from the (LLM-controlled) tool argument: an attacker could otherwise pass
# ``{"tenant_id": "victim"}`` and read another tenant's documents.
AUTHORITATIVE_FILTER_KEYS: frozenset[str] = frozenset(
    {
        "tenant_id", "owner_id", "acl", "acls", "tenant", "owner",
        "user_roles", "allowed_roles", "department", "acl_principals",
    }
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
    """Return the verified tenant for this tool call (fail-closed default).

    Never derives a tenant from ``owner_id`` (a user principal) or from the
    LLM-supplied ``filters`` argument.
    """
    context = get_tool_execution_context()
    if context and context.tenant_id:
        return context.tenant_id
    return str(getattr(get_settings(), "rag_default_tenant_id", "public"))


def _build_effective_filters(filters: dict[str, Any] | None) -> dict[str, Any]:
    """Strip caller-supplied auth keys and pin the verified authorization scope."""
    effective = {
        key: value
        for key, value in (filters or {}).items()
        if key not in AUTHORITATIVE_FILTER_KEYS
    }
    # Plain assignment (not setdefault): the authoritative value always wins.
    effective["tenant_id"] = _authoritative_tenant_id()
    context = get_tool_execution_context()
    if context is None:
        if is_strict_env(get_settings()):
            raise PermissionError("Verified RAG authorization context is required in production.")
        effective["user_roles"] = ["guest"]
        effective["department"] = "public"
        effective["acl_principals"] = [effective["tenant_id"], "public"]
    else:
        roles = sorted(set(context.user_roles or ("guest",)))
        department = str(context.department or "public").lower()
        effective["user_roles"] = roles
        effective["department"] = department
        effective["acl_principals"] = sorted(
            {
                str(context.owner_id or effective["tenant_id"]),
                department,
                *(f"role:{role}" for role in roles),
                "public",
            }
        )
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


def format_citations(citations: list[dict[str, Any]]) -> str:
    """Serialize structured citations into a parseable tool-output block."""
    payload = json.dumps({"citations": citations}, ensure_ascii=False, sort_keys=True)
    return f"{CITATION_MARKER_START}\n{payload}\n{CITATION_MARKER_END}"


def extract_citations(text: str) -> list[dict[str, Any]]:
    """Parse and return only structurally valid citations from tool output."""
    if not text or CITATION_MARKER_START not in text or CITATION_MARKER_END not in text:
        return []
    try:
        block = text.split(CITATION_MARKER_START, 1)[1].split(CITATION_MARKER_END, 1)[0]
        parsed = json.loads(block.strip())
        if not isinstance(parsed, dict):
            return []
        citations = parsed.get("citations")
        if not isinstance(citations, list) or not citations:
            return []
        valid: list[dict[str, Any]] = []
        for citation in citations:
            if (
                isinstance(citation, dict)
                and str(citation.get("doc_id", "")).strip()
                and (
                    str(citation.get("file_name", "")).strip()
                    or str(citation.get("source", "")).strip()
                )
            ):
                valid.append(dict(citation))
        return valid
    except (ValueError, TypeError, IndexError):
        return []


NO_CONTEXT_MARKER = "No relevant enterprise documentation found"


def validate_citations(text: str) -> bool:
    """Return True when ``text`` contains at least one well-formed citation."""
    return bool(extract_citations(text))


def preserve_rag_citations(answer: str, tool_outputs: list[str]) -> str:
    """Preserve structured citations or empty-retrieval signal from RAG tool outputs.

    When an LLM summarizes retrieved chunks without copying the trailing
    ``[CITATIONS]...[/CITATIONS]`` JSON block, deterministically re-attach the
    deduplicated citation records observed in ``tool_outputs``. When retrieval
    returned zero documents, ensure ``NO_CONTEXT_MARKER`` is preserved so
    downstream gates distinguish empty retrieval from uncited hallucinations.
    """
    if validate_citations(answer) or NO_CONTEXT_MARKER in answer:
        return answer

    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    saw_no_context = False
    for tool_text in tool_outputs:
        if not tool_text:
            continue
        for citation in extract_citations(tool_text):
            key = (str(citation.get("doc_id")), str(citation.get("source") or citation.get("file_name")))
            deduped[key] = citation
        if NO_CONTEXT_MARKER in tool_text:
            saw_no_context = True

    if deduped:
        suffix = format_citations(list(deduped.values()))
        return f"{answer.rstrip()}\n\n{suffix}" if answer.strip() else suffix
    if saw_no_context:
        return (
            f"{answer.rstrip()} ({NO_CONTEXT_MARKER})"
            if answer.strip()
            else f"{NO_CONTEXT_MARKER}."
        )
    return answer


def _build_shared_cache_filters(
    effective_filters: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Derive a tenant/role/department cache filter without per-user ``owner_id``."""
    context = get_tool_execution_context()
    owner_id = str(context.owner_id) if context and context.owner_id else None
    shared = dict(effective_filters)
    tenant_id = str(shared.get("tenant_id") or "")
    roles = list(shared.get("user_roles") or ["guest"])
    department = str(shared.get("department") or "public").lower()
    if owner_id and context is not None:
        shared["acl_principals"] = sorted(
            {
                tenant_id,
                department,
                *(f"role:{role}" for role in roles),
                "public",
            }
            - {""}
        )
    return shared, owner_id


def _results_are_shared_cache_safe(
    reranked: list[Any],
    shared_filters: dict[str, Any],
    owner_id: str | None,
) -> bool:
    """True only when every returned node is accessible without user-private owner_id ACL."""
    if not owner_id:
        return True
    shared_principals = set(map(str, shared_filters.get("acl_principals") or ()))
    for item in reranked:
        metadata = getattr(item.node, "metadata", {}) or {}
        acl = metadata.get("acl")
        if isinstance(acl, (list, tuple, set)):
            acl_set = set(map(str, acl))
            if owner_id in acl_set and not shared_principals.intersection(acl_set):
                return False
    return True


def search_policy(
    query: str,
    filters: dict[str, Any] | None = None,
    *,
    force_heuristic_reranker: bool = False,
) -> str:
    """Queries internal documentation using hybrid search, cross-encoder reranking, and cache.

    Authorization-relevant filter keys supplied by the caller are ignored; the
    tenant always comes from the verified execution context. The returned text
    contains a structured ``[CITATIONS]`` JSON block suitable for validation
    with :func:`validate_citations`.
    """
    cache = get_rag_cache()
    effective_filters = _build_effective_filters(filters)
    shared_filters, owner_id = _build_shared_cache_filters(effective_filters)
    shared_scope = json.dumps(shared_filters, sort_keys=True, ensure_ascii=False, default=str)
    private_scope = json.dumps(effective_filters, sort_keys=True, ensure_ascii=False, default=str)

    used_private_acl = False

    def _do_retrieve() -> str:
        nonlocal used_private_acl
        retriever = get_hybrid_retriever()
        reranker = CrossEncoderReranker(
            top_n=3,
            use_neural=not force_heuristic_reranker,
        )

        raw_nodes = retriever.retrieve(query, top_k=6, filters=effective_filters)
        if getattr(raw_nodes, "degraded", False) and not raw_nodes:
            reasons = ", ".join(getattr(raw_nodes, "degraded_reasons", [])) or "unknown"
            raise RuntimeError(f"RAG retrieval degraded and returned no results ({reasons}).")
        if not raw_nodes:
            return f"{NO_CONTEXT_MARKER} for query: '{query}'."

        reranked = reranker.rerank(query, raw_nodes)
        if reranker.mode == "neural" and reranked:
            max_score = max(float(item.score or 0.0) for item in reranked)
            if max_score < 0.01:
                return f"{NO_CONTEXT_MARKER} for query: '{query}'."
            min_keep = 0.02 if max_score >= 0.05 else max_score * 0.5
            reranked = [item for item in reranked if float(item.score or 0.0) >= min_keep]

        if not _results_are_shared_cache_safe(reranked, shared_filters, owner_id):
            used_private_acl = True

        results: list[str] = []
        citations: list[dict[str, Any]] = []
        seen_texts: set[str] = set()
        for item in reranked:
            node_text = (
                item.node.get_content()
                if hasattr(item.node, "get_content")
                else getattr(item.node, "text", "")
            )
            normalized_text = " ".join(node_text.split())
            if not normalized_text or normalized_text in seen_texts:
                continue
            seen_texts.add(normalized_text)
            rank_idx = len(results) + 1
            metadata = getattr(item.node, "metadata", {}) or {}
            source = str(metadata.get("file_name", "internal_doc"))
            score = f"{item.score:.3f}" if item.score is not None else "N/A"
            citations.append(_build_citation(item, rank_idx))
            results.append(
                f"--- Document Source [{rank_idx}]: {source} (Score: {score}) ---\n{node_text.strip()}"
            )

        return "\n\n".join(results) + "\n\n" + format_citations(citations)

    domain = (
        (effective_filters.get("domain") or effective_filters.get("category") or "general")
        if effective_filters
        else "general"
    )
    if shared_scope != private_scope:
        cached_private = cache.get_semantic(
            query, domain=str(domain), top_k=3, scope=private_scope
        )
        if isinstance(cached_private, str):
            return cached_private

    # Compute first or reuse shared cache; if any document required owner-only ACL,
    # write under private_scope and invalidate shared entry so private docs stay private.
    computed = cache.get_or_compute(
        query, _do_retrieve, top_k=3, domain=str(domain), scope=shared_scope
    )
    if used_private_acl and shared_scope != private_scope:
        cache.invalidate_by_source(shared_scope)
        cache.set_semantic(query, computed, domain=str(domain), top_k=3, scope=private_scope)
    return computed


async def asearch_policy(
    query: str,
    filters: dict[str, Any] | None = None,
    *,
    force_heuristic_reranker: bool = False,
) -> str:
    """Asynchronously queries internal documentation, offloading CPU / disk retrieval to worker thread."""
    return await asyncio.to_thread(
        search_policy,
        query,
        filters,
        force_heuristic_reranker=force_heuristic_reranker,
    )


def get_rag_tool() -> StructuredTool:
    """Returns the LangChain StructuredTool for search_policy supporting both sync and non-blocking async execution."""
    return StructuredTool(
        name="search_policy",
        description="Search enterprise policies, internal guidelines, and technical documentation.",
        func=search_policy,
        coroutine=asearch_policy,
        args_schema=SearchPolicyInput,
    )
