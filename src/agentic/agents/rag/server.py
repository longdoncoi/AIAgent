"""A2A Server for RAGAgent (Port 9101)."""

import argparse

import uvicorn

from src.agentic.a2a.cards import RAG_AGENT_CARD
from src.agentic.a2a.executor import create_a2a_server
from src.agentic.agents.rag.graph import get_rag_graph

graph = get_rag_graph()
app = create_a2a_server(card=RAG_AGENT_CARD, graph=graph)


def main() -> None:
    parser = argparse.ArgumentParser(description="RAGAgent A2A Server")
    parser.add_argument("--port", type=int, default=9101, help="Port to listen on")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host interface")
    args = parser.parse_args()

    uvicorn.run("src.agentic.agents.rag.server:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
