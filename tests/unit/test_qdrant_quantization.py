"""Unit tests for Qdrant Vector Store with HNSW on-disk mmap and INT8 Scalar Quantization."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from qdrant_client.http import models as qmodels

from src.agentic.core.config import Settings
from src.agentic.rag.ingestion import ingest_documents
from src.agentic.rag.retriever import HybridRetriever, get_qdrant_vector_store


def test_qdrant_disabled_by_default():
    """When enable_qdrant is False and no client injected, returns None."""
    with patch("src.agentic.rag.retriever.get_settings") as mock_settings:
        mock_settings.return_value = Settings(enable_qdrant=False)
        store = get_qdrant_vector_store()
        assert store is None


def test_qdrant_creation_with_hnsw_mmap_and_scalar_quantization():
    """Verifies collection creation with on_disk HNSW mmap and INT8 Scalar Quantization."""
    mock_client = MagicMock()
    mock_collections = MagicMock()
    mock_collections.collections = []  # No collections yet
    mock_client.get_collections.return_value = mock_collections

    store = get_qdrant_vector_store(
        collection_name="test_agentic_knowledge",
        embed_dim=768,
        client=mock_client,
    )
    assert store is not None
    mock_client.create_collection.assert_called_once()

    call_kwargs = mock_client.create_collection.call_args[1]
    assert call_kwargs["collection_name"] == "test_agentic_knowledge"

    # Verify VectorParams on_disk=True (vectors memory-mapped on disk)
    vector_params = call_kwargs["vectors_config"]
    assert isinstance(vector_params, qmodels.VectorParams)
    assert vector_params.size == 768
    assert vector_params.on_disk is True

    # Verify HNSW on_disk=True
    hnsw_config = call_kwargs["hnsw_config"]
    assert isinstance(hnsw_config, qmodels.HnswConfigDiff)
    assert hnsw_config.on_disk is True
    assert hnsw_config.m == 16
    assert hnsw_config.ef_construct == 100

    # Verify INT8 Scalar Quantization
    quant_config = call_kwargs["quantization_config"]
    assert isinstance(quant_config, qmodels.ScalarQuantization)
    assert quant_config.scalar.type == qmodels.ScalarType.INT8
    assert quant_config.scalar.always_ram is True


def test_qdrant_reuse_existing_collection():
    """If collection exists, it is reused without calling create_collection."""
    mock_client = MagicMock()
    mock_col = MagicMock()
    mock_col.name = "existing_collection"
    mock_collections = MagicMock()
    mock_collections.collections = [mock_col]
    mock_client.get_collections.return_value = mock_collections

    store = get_qdrant_vector_store(
        collection_name="existing_collection",
        embed_dim=768,
        client=mock_client,
    )
    assert store is not None
    mock_client.create_collection.assert_not_called()


def test_qdrant_retriever_load_storage_integration(tmp_path: Path):
    """Verifies HybridRetriever correctly wires QdrantVectorStore when enabled."""
    mock_qdrant_store = MagicMock()

    with patch("src.agentic.rag.retriever.get_qdrant_vector_store", return_value=mock_qdrant_store):
        with patch("llama_index.core.VectorStoreIndex.from_vector_store") as mock_from_store:
            mock_index = MagicMock()
            mock_from_store.return_value = mock_index

            retriever = HybridRetriever(index_dir=tmp_path)
            assert retriever._index is mock_index
            mock_from_store.assert_called_once()


def test_qdrant_ingestion_integration(tmp_path: Path):
    """Verifies ingest_documents uses QdrantVectorStore when available."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "sample.txt").write_text("Policy: Security rules for agentic platform.", encoding="utf-8")
    index_dir = tmp_path / "index"

    mock_qdrant_store = MagicMock()

    with patch("src.agentic.rag.retriever.get_qdrant_vector_store", return_value=mock_qdrant_store):
        with patch("src.agentic.rag.ingestion.VectorStoreIndex") as mock_vsi:
            leaf_count = ingest_documents(raw_data_dir=raw_dir, index_persist_dir=index_dir)
            assert leaf_count > 0
            mock_vsi.assert_called_once()


def test_qdrant_connection_failure_fallback(tmp_path: Path):
    """Verifies exception in Qdrant setup falls back gracefully to None / local storage."""
    mock_client = MagicMock()
    mock_client.get_collections.side_effect = ConnectionError("Qdrant unreachable")

    store = get_qdrant_vector_store(client=mock_client)
    assert store is None
