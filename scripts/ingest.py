"""CLI script to ingest documents from data/raw/ into LlamaIndex."""

import argparse
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import nltk

    _nltk_dir = Path.home() / "nltk_data"
    _nltk_dir.mkdir(parents=True, exist_ok=True)
    if str(_nltk_dir) not in nltk.data.path:
        nltk.data.path.insert(0, str(_nltk_dir))
    for _r in ["stopwords", "punkt", "punkt_tab"]:
        try:
            nltk.download(_r, download_dir=str(_nltk_dir), quiet=True)
        except Exception:
            pass
except Exception:
    pass

from src.agentic.rag.ingestion import ingest_documents


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest documents into LlamaIndex")
    parser.add_argument("--data-dir", type=str, default="data/raw", help="Path to raw documents directory")
    parser.add_argument("--index-dir", type=str, default="data/index", help="Path to persist index directory")
    args = parser.parse_args()

    print(f"Starting document ingestion from '{args.data_dir}' into '{args.index_dir}'...")
    leaf_count = ingest_documents(raw_data_dir=args.data_dir, index_persist_dir=args.index_dir)
    print(f"Ingestion completed successfully. {leaf_count} leaf nodes indexed.")


if __name__ == "__main__":
    main()
