"""Document ingestion with Hierarchical Chunking (Parent 1024 / Child 256)."""

import contextlib
import hashlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from llama_index.core import Document, StorageContext, VectorStoreIndex
from llama_index.core.node_parser import HierarchicalNodeParser, get_leaf_nodes
from llama_index.core.schema import BaseNode
from llama_index.core.storage.docstore import SimpleDocumentStore

from src.agentic.core.config import get_settings

logger = logging.getLogger(__name__)

# Dedicated namespace keeps generated IDs stable across processes and ingestion
# runs. A bare sha256 hex string is NOT a valid Qdrant point ID (uint64/UUID).
_NODE_ID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
_MANIFEST_NAME = "ingestion_manifest.json"


def _setup_nltk() -> None:
    """Pre-configures NLTK data directory to avoid permissions and lookup errors in CI/CD."""
    try:
        import nltk

        nltk_dir = Path.home() / "nltk_data"
        nltk_dir.mkdir(parents=True, exist_ok=True)
        if str(nltk_dir) not in nltk.data.path:
            nltk.data.path.insert(0, str(nltk_dir))
        for res in ["stopwords", "punkt", "punkt_tab"]:
            with contextlib.suppress(Exception):
                nltk.download(res, download_dir=str(nltk_dir), quiet=True)
    except Exception as e:
        logger.debug("NLTK setup note: %s", e)


def _sample_data_allowed() -> bool:
    """Whether fabricating sample documents is permitted in this environment.

    Production/staging never get synthetic knowledge: an empty corpus must
    fail loudly instead of silently seeding a hardcoded policy document.
    """
    settings = get_settings()
    environment = str(getattr(settings, "environment", "development")).lower()
    explicit_flag = bool(getattr(settings, "allow_sample_rag_data", False))
    return environment in {"development", "test"} or explicit_flag


def _trusted_tenant_id(explicit_tenant_id: str | None = None) -> str:
    """Resolve the ingestion tenant from a trusted source only.

    ``explicit_tenant_id`` is an argument of the *ingestion pipeline* (called
    by trusted workflows), never an LLM tool argument. If absent, the config
    default is authoritative.
    """
    if explicit_tenant_id:
        return explicit_tenant_id
    return str(getattr(get_settings(), "rag_default_tenant_id", "public"))


def load_documents_from_dir(dir_path: Path) -> list[Document]:
    """Loads all text and markdown documents from directory."""
    docs: list[Document] = []
    if not dir_path.exists():
        logger.warning("Data directory %s does not exist.", dir_path)
        return docs

    base_resolved = dir_path.resolve()
    for file_path in sorted(dir_path.rglob("*")):
        if file_path.suffix.lower() in [".txt", ".md", ".json"]:
            try:
                rel_parts = file_path.resolve().relative_to(base_resolved).parts
                inferred_tenant: str | None = None
                if len(rel_parts) >= 3 and rel_parts[0] == "tenants" and rel_parts[1]:
                    inferred_tenant = rel_parts[1]
                with open(file_path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
                    if text.strip():
                        meta: dict[str, Any] = {
                            "file_name": file_path.name,
                            "file_path": str(file_path),
                            "file_type": file_path.suffix,
                        }
                        if inferred_tenant is not None:
                            meta["inferred_tenant_id"] = inferred_tenant
                        doc = Document(
                            text=text,
                            metadata=meta,
                        )
                        docs.append(doc)
            except Exception as e:
                logger.error("Error reading %s: %s", file_path, e)
    return docs


def _attach_security_metadata(
    documents: list[Document],
    tenant_id: str,
    acl: list[str] | None = None,
    department: str | None = None,
    allowed_roles: list[str] | None = None,
) -> None:
    """Stamp authoritative tenant/ACL metadata onto documents (in place)."""
    for doc in documents:
        metadata = dict(getattr(doc, "metadata", {}) or {})
        doc_tenant = str(metadata.pop("inferred_tenant_id", None) or tenant_id)
        metadata["tenant_id"] = doc_tenant
        metadata["acl"] = list(acl) if acl else [doc_tenant]
        metadata["department"] = str(department or "*").lower()
        metadata["allowed_roles"] = sorted(set(allowed_roles or ["*"]))
        doc.metadata = metadata


def _assign_deterministic_node_ids(nodes: list[BaseNode], tenant_id: str) -> None:
    """Assign stable UUIDv5 IDs and rewrite parent/child relationships."""
    id_map: dict[str, str] = {}
    for idx, node in enumerate(nodes):
        file_key = str(node.metadata.get("file_name", ""))
        node_tenant = str(node.metadata.get("tenant_id") or tenant_id)
        node_content = node.get_content()
        old_id = str(node.id_)
        new_id = str(
            uuid.uuid5(
                _NODE_ID_NAMESPACE,
                f"{node_tenant}:{file_key}:{idx}:{node_content}",
            )
        )
        id_map[old_id] = new_id
        node.id_ = new_id

    # RelatedNodeInfo.node_id is mutable; repoint every relationship that
    # referenced an old generated ID so auto-merging still works.
    for node in nodes:
        for related_value in node.relationships.values():
            related_items = (
                related_value if isinstance(related_value, list) else [related_value]
            )
            for related in related_items:
                related_id = getattr(related, "node_id", None)
                if related_id is not None and str(related_id) in id_map:
                    related.node_id = id_map[str(related_id)]



def deduplicate_leaf_nodes(nodes: list[BaseNode], tenant_id: str) -> list[BaseNode]:
    """Remove duplicate leaf chunks within each node's tenant scope using SHA-256 content hashes."""
    seen_hashes: set[str] = set()
    unique_nodes: list[BaseNode] = []
    skipped = 0
    for node in nodes:
        normalized = " ".join(node.get_content().split())
        node_tenant = str((getattr(node, "metadata", {}) or {}).get("tenant_id") or tenant_id)
        digest = hashlib.sha256(f"{node_tenant}\x00{normalized}".encode()).hexdigest()
        if digest in seen_hashes:
            skipped += 1
            continue
        seen_hashes.add(digest)
        metadata = dict(getattr(node, "metadata", {}) or {})
        metadata["content_sha256"] = digest
        node.metadata = metadata
        unique_nodes.append(node)
    if skipped:
        logger.info("Deduplicated %s duplicate chunk(s) during RAG ingestion.", skipped)
    return unique_nodes


def _load_manifest(persist_dir: Path) -> dict[str, Any]:
    """Load the source-file -> point-ID manifest, tolerating legacy/corrupt files."""
    path = persist_dir / _MANIFEST_NAME
    if not path.exists():
        return {"version": 1, "sources": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("sources"), dict):
            data.setdefault("version", 1)
            return data
    except (ValueError, OSError) as exc:
        logger.warning("Ignoring unreadable ingestion manifest at %s: %s", path, exc)
    return {"version": 1, "sources": {}}


def _atomic_persist_storage_context(storage_context: StorageContext, persist_dir: Path) -> None:
    """Persist LlamaIndex docstore/index files atomically via a staging directory."""
    import tempfile

    persist_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(persist_dir.parent), prefix=".index_staging_") as tmp:
        staging_dir = Path(tmp)
        storage_context.persist(persist_dir=str(staging_dir))
        for item in staging_dir.iterdir():
            if item.is_file():
                target = persist_dir / item.name
                tmp_target = persist_dir / f".{item.name}.tmp"
                tmp_target.write_bytes(item.read_bytes())
                tmp_target.replace(target)


def _save_manifest(persist_dir: Path, manifest: dict[str, Any]) -> None:
    """Persist the source-file -> point-ID manifest atomically."""
    path = persist_dir / _MANIFEST_NAME
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    tmp_path.replace(path)


def _delete_point_ids(vector_store: Any | None, point_ids: list[str]) -> None:
    """Delete stale Qdrant points by ID; no-op when no store is configured."""
    if vector_store is None or not point_ids:
        return
    try:
        vector_store.delete_nodes(node_ids=point_ids)
        logger.info("Deleted %s stale vector points on re-ingest.", len(point_ids))
    except Exception as exc:
        logger.warning("Could not delete stale vector points: %s", exc)


def _prune_docstore_nodes(docstore: SimpleDocumentStore, stale_ids: list[str]) -> int:
    """Remove deleted or superseded node IDs (and orphaned parents) from docstore."""
    if not stale_ids:
        return 0
    docs = getattr(docstore, "docs", None)
    if not isinstance(docs, dict):
        return 0

    stale_set = {str(nid) for nid in stale_ids if nid}
    removed = 0
    for nid in list(stale_set):
        if nid in docs:
            try:
                docstore.delete_document(nid, raise_error=False)
            except Exception:
                docs.pop(nid, None)
            removed += 1

    # Prune parent/intermediate nodes whose child references are no longer in docstore.
    for pid, node in list(docs.items()):
        child_rels = getattr(node, "child_nodes", None) or []
        if not child_rels:
            continue
        child_ids = [
            str(getattr(rel, "node_id", ""))
            for rel in child_rels
            if getattr(rel, "node_id", None)
        ]
        if child_ids and all(cid not in docs for cid in child_ids):
            try:
                docstore.delete_document(pid, raise_error=False)
            except Exception:
                docs.pop(pid, None)
            removed += 1
    return removed


def delete_source(
    source_key: str,
    index_persist_dir: Path | str | None = None,
    vector_store: Any | None = None,
) -> int:
    """Delete all tracked vector points and docstore nodes for one source file."""
    settings = get_settings()
    persist_dir = (
        Path(index_persist_dir) if index_persist_dir else settings.project_root / "data" / "index"
    )
    manifest = _load_manifest(persist_dir)
    point_ids = [str(pid) for pid in manifest.get("sources", {}).pop(source_key, [])]
    store = vector_store
    if store is None:
        from src.agentic.rag.retriever import get_qdrant_vector_store

        store = get_qdrant_vector_store()
    _delete_point_ids(store, point_ids)

    docstore_path = persist_dir / "docstore.json"
    if point_ids and docstore_path.exists():
        try:
            docstore = SimpleDocumentStore.from_persist_dir(persist_dir=str(persist_dir))
            if _prune_docstore_nodes(docstore, point_ids) > 0:
                storage_ctx = StorageContext.from_defaults(docstore=docstore)
                _atomic_persist_storage_context(storage_ctx, persist_dir)
        except Exception as exc:
            logger.warning("Could not prune docstore during delete_source: %s", exc)

    _save_manifest(persist_dir, manifest)
    return len(point_ids)


def ingest_single_document(
    file_path: Path | str,
    index_persist_dir: Path | str | None = None,
    *,
    tenant_id: str | None = None,
    acl: list[str] | None = None,
    department: str | None = None,
    allowed_roles: list[str] | None = None,
) -> int:
    """Incrementally ingest a single document (CDC delta) without re-embedding the whole corpus."""
    settings = get_settings()
    target = Path(file_path)
    persist_dir = (
        Path(index_persist_dir) if index_persist_dir else settings.project_root / "data" / "index"
    )
    persist_dir.mkdir(parents=True, exist_ok=True)
    if not target.is_file():
        return 0

    text = target.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return 0

    effective_tenant = _trusted_tenant_id(tenant_id)
    doc = Document(
        text=text,
        metadata={
            "file_name": target.name,
            "file_path": str(target),
            "file_type": target.suffix,
        },
    )
    _attach_security_metadata([doc], effective_tenant, acl, department, allowed_roles)

    node_parser = HierarchicalNodeParser.from_defaults(chunk_sizes=[1024, 256], chunk_overlap=32)
    nodes = node_parser.get_nodes_from_documents([doc])
    _assign_deterministic_node_ids(nodes, effective_tenant)
    leaf_nodes = deduplicate_leaf_nodes(get_leaf_nodes(nodes), effective_tenant)

    docstore_path = persist_dir / "docstore.json"
    if docstore_path.exists():
        try:
            docstore = SimpleDocumentStore.from_persist_dir(persist_dir=str(persist_dir))
        except Exception:
            docstore = SimpleDocumentStore()
    else:
        docstore = SimpleDocumentStore()
    docstore.add_documents(nodes)

    from src.agentic.rag.embedding import configure_embedding_model
    from src.agentic.rag.retriever import get_qdrant_vector_store

    qdrant_store = get_qdrant_vector_store()
    storage_context = (
        StorageContext.from_defaults(docstore=docstore, vector_store=qdrant_store)
        if qdrant_store is not None
        else StorageContext.from_defaults(docstore=docstore)
    )
    configure_embedding_model()

    manifest = _load_manifest(persist_dir)
    sources = manifest.setdefault("sources", {})
    source_key = str(target)
    previous = {str(pid) for pid in sources.get(source_key, [])}
    new_ids = {str(node.id_) for node in leaf_nodes}
    stale_ids = sorted(previous - new_ids)
    sources[source_key] = sorted(new_ids)
    _prune_docstore_nodes(docstore, stale_ids)
    _delete_point_ids(qdrant_store, stale_ids)

    _ = VectorStoreIndex(leaf_nodes, storage_context=storage_context)
    _atomic_persist_storage_context(storage_context, persist_dir)
    _save_manifest(persist_dir, manifest)
    return len(leaf_nodes)


def ingest_documents(
    raw_data_dir: Path | str | None = None,
    index_persist_dir: Path | str | None = None,
    *,
    tenant_id: str | None = None,
    acl: list[str] | None = None,
    department: str | None = None,
    allowed_roles: list[str] | None = None,
) -> int:
    """Ingests documents, builds hierarchical chunks, and persists index.

    Security metadata comes only from trusted configuration (or an explicit
    pipeline argument), never from user-supplied tool data. Re-ingesting a
    source deletes its previously tracked -- now stale -- vector points.
    """
    settings = get_settings()
    data_dir = Path(raw_data_dir) if raw_data_dir else settings.project_root / "data" / "raw"
    persist_dir = (
        Path(index_persist_dir) if index_persist_dir else settings.project_root / "data" / "index"
    )
    persist_dir.mkdir(parents=True, exist_ok=True)

    effective_tenant = _trusted_tenant_id(tenant_id)
    documents = load_documents_from_dir(data_dir)
    if not documents:
        if not _sample_data_allowed():
            raise RuntimeError(
                f"No ingestion documents found in {data_dir}. Refusing to seed fabricated "
                "sample knowledge outside development/test."
            )
        logger.info("No documents found in %s. Writing development-only sample knowledge.", data_dir)
        sample_doc = Document(
            text=(
                "# Enterprise Security & Execution Policy\n\n"
                "## 1. Code Execution Policy\n"
                "All Python execution must run within isolated sandboxes with a 30-second timeout.\n"
                "Bash execution is classified as high-risk and requires explicit Human-In-The-Loop approval.\n\n"
                "## 2. Data Protection\n"
                "Customer PII including email addresses, phone numbers, and API tokens must never be logged or sent "
                "to external non-approved APIs. All outputs must be sanitized before presentation."
            ),
            metadata={"file_name": "sample_security_policy.md"},
        )
        documents = [sample_doc]

    _attach_security_metadata(documents, effective_tenant, acl, department, allowed_roles)

    # Hierarchical Node Parser: 1024 parent chunk, 256 child chunk
    node_parser = HierarchicalNodeParser.from_defaults(
        chunk_sizes=[1024, 256],
        chunk_overlap=32,
    )
    nodes = node_parser.get_nodes_from_documents(documents)
    _assign_deterministic_node_ids(nodes, effective_tenant)

    leaf_nodes = deduplicate_leaf_nodes(get_leaf_nodes(nodes), effective_tenant)

    # Setup docstore and storage context
    docstore = SimpleDocumentStore()
    docstore.add_documents(nodes)

    from src.agentic.rag.retriever import get_qdrant_vector_store

    qdrant_store = get_qdrant_vector_store()
    if qdrant_store is not None:
        storage_context = StorageContext.from_defaults(
            docstore=docstore, vector_store=qdrant_store
        )
        logger.info(
            "Using QdrantVectorStore with HNSW on-disk mmap and INT8 Scalar Quantization for ingestion."
        )
    else:
        storage_context = StorageContext.from_defaults(docstore=docstore)

    # Embedding model configuration -- shared with retrieval, fail-closed in production.
    from src.agentic.rag.embedding import configure_embedding_model

    configure_embedding_model()

    # Delete stale vectors for every source being re-ingested. New IDs replace
    # the manifest entry; IDs that disappeared from the source are removed.
    manifest = _load_manifest(persist_dir)
    sources = manifest.setdefault("sources", {})
    new_ids_by_source: dict[str, set[str]] = {}
    for node in leaf_nodes:
        source_key = str(
            node.metadata.get("file_path") or node.metadata.get("file_name") or "unknown"
        )
        new_ids_by_source.setdefault(source_key, set()).add(str(node.id_))

    stale_ids: list[str] = []
    for source_key, new_ids in new_ids_by_source.items():
        previous = {str(pid) for pid in sources.get(source_key, [])}
        stale_ids.extend(sorted(previous - new_ids))
        sources[source_key] = sorted(new_ids)
    _delete_point_ids(qdrant_store, stale_ids)

    # Build Vector Store Index over leaf nodes
    _ = VectorStoreIndex(
        leaf_nodes,
        storage_context=storage_context,
    )

    _atomic_persist_storage_context(storage_context, persist_dir)
    _save_manifest(persist_dir, manifest)

    # Index Knowledge Graph triples from documents
    if settings.enable_knowledge_graph:
        try:
            from src.agentic.rag.kg import get_knowledge_graph_store

            kg = get_knowledge_graph_store()
            total_triples = 0
            for doc in documents:
                file_name = (
                    doc.metadata.get("file_name", "doc") if hasattr(doc, "metadata") else "doc"
                )
                total_triples += kg.index_text(
                    doc.text,
                    source_doc=file_name,
                    metadata={
                        "tenant_id": doc.metadata.get("tenant_id"),
                        "department": doc.metadata.get("department"),
                        "allowed_roles": doc.metadata.get("allowed_roles"),
                        "acl": doc.metadata.get("acl"),
                    },
                )
            logger.info(
                "Successfully extracted and indexed %s triples into Knowledge Graph.",
                total_triples,
            )
        except Exception as e:
            logger.debug("Knowledge graph indexing note: %s", e)

    logger.info(
        "Successfully ingested %s documents, %s total nodes (%s leaf nodes).",
        len(documents),
        len(nodes),
        len(leaf_nodes),
    )
    return len(leaf_nodes)
