"""Document ingestion with Hierarchical Chunking (Parent 1024 / Child 256)."""

import json
import logging
import socket
from pathlib import Path
from urllib.parse import urlparse

from llama_index.core import Document, StorageContext, VectorStoreIndex
from llama_index.core.node_parser import HierarchicalNodeParser, get_leaf_nodes
from llama_index.core.storage.docstore import SimpleDocumentStore

from src.agentic.core.config import get_settings

logger = logging.getLogger(__name__)


def configure_embedding_model() -> None:
    """Configure the real embedding service in production; mock only for local development."""
    settings = get_settings()
    parsed = urlparse(settings.llama_embed_base_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    available = settings.environment == "production"
    if not available:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                available = True
        except OSError:
            available = False

    from llama_index.core import Settings as LlamaSettings

    if available:
        from llama_index.embeddings.openai import OpenAIEmbedding

        LlamaSettings.embed_model = OpenAIEmbedding(
            api_base=settings.llama_embed_base_url,
            api_key=settings.litellm_api_key,
            model_name=settings.default_embedding_model,
            timeout=10.0,
            max_retries=2,
        )
        return
    from llama_index.core.embeddings.mock_embed_model import MockEmbedding

    logger.info("Configured MockEmbedding for non-production local development.")
    LlamaSettings.embed_model = MockEmbedding(embed_dim=768)


def _setup_nltk() -> None:
    """Pre-configures NLTK data directory to avoid permissions and lookup errors in CI/CD."""
    try:
        import nltk

        nltk_dir = Path.home() / "nltk_data"
        nltk_dir.mkdir(parents=True, exist_ok=True)
        if str(nltk_dir) not in nltk.data.path:
            nltk.data.path.insert(0, str(nltk_dir))
        for res in ["stopwords", "punkt", "punkt_tab"]:
            try:
                nltk.download(res, download_dir=str(nltk_dir), quiet=True)
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"NLTK setup note: {e}")

def load_documents_from_dir(dir_path: Path) -> list[Document]:
    """Loads all text and markdown documents from directory."""
    docs: list[Document] = []
    if not dir_path.exists():
        logger.warning(f"Data directory {dir_path} does not exist.")
        return docs

    for file_path in dir_path.rglob("*"):
        if file_path.name.endswith(".metadata.json"):
            continue
        if file_path.suffix.lower() in [".txt", ".md", ".json"]:
            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
                    if text.strip():
                        metadata = {
                            "file_name": file_path.name,
                            "file_path": str(file_path),
                            "file_type": file_path.suffix,
                            "visibility": "shared",
                        }
                        sidecar = file_path.with_suffix(file_path.suffix + ".metadata.json")
                        if sidecar.exists():
                            sidecar_data = json.loads(sidecar.read_text(encoding="utf-8"))
                            if isinstance(sidecar_data, dict):
                                metadata.update(sidecar_data)
                        doc = Document(
                            text=text,
                            metadata=metadata,
                        )
                        docs.append(doc)
            except Exception as e:
                logger.error(f"Error reading {file_path}: {e}")
    return docs


def ingest_documents(
    raw_data_dir: Path | str | None = None,
    index_persist_dir: Path | str | None = None,
) -> int:
    """Ingests documents, builds hierarchical chunks, and persists index."""
    settings = get_settings()
    data_dir = Path(raw_data_dir) if raw_data_dir else settings.project_root / "data" / "raw"
    persist_dir = (
        Path(index_persist_dir) if index_persist_dir else settings.project_root / "data" / "index"
    )
    persist_dir.mkdir(parents=True, exist_ok=True)

    documents = load_documents_from_dir(data_dir)
    if not documents:
        logger.info(f"No documents found in {data_dir}. Writing sample knowledge.")
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
            metadata={"file_name": "sample_security_policy.md", "visibility": "shared"},
        )
        documents = [sample_doc]

    # Hierarchical Node Parser: 1024 parent chunk, 256 child chunk
    node_parser = HierarchicalNodeParser.from_defaults(
        chunk_sizes=[1024, 256],
        chunk_overlap=32,
    )
    nodes = node_parser.get_nodes_from_documents(documents)
    leaf_nodes = get_leaf_nodes(nodes)

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

    configure_embedding_model()

    # Build Vector Store Index over leaf nodes
    _ = VectorStoreIndex(
        leaf_nodes,
        storage_context=storage_context,
    )

    storage_context.persist(persist_dir=str(persist_dir))

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
                total_triples += kg.index_text(doc.text, source_doc=file_name)
            logger.info(
                f"Successfully extracted and indexed {total_triples} triples into Knowledge Graph."
            )
        except Exception as e:
            logger.debug(f"Knowledge graph indexing note: {e}")

    logger.info(
        f"Successfully ingested {len(documents)} documents, {len(nodes)} total nodes ({len(leaf_nodes)} leaf nodes)."
    )
    return len(leaf_nodes)
