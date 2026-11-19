"""A2A Server for CodingAgent (Port 9102)."""

import argparse

import uvicorn

from src.agentic.a2a.cards import CODING_AGENT_CARD
from src.agentic.a2a.executor import create_a2a_server
from src.agentic.agents.coding.graph import get_coding_graph

graph = get_coding_graph()
app = create_a2a_server(card=CODING_AGENT_CARD, graph=graph)


def main() -> None:
    parser = argparse.ArgumentParser(description="CodingAgent A2A Server")
    parser.add_argument("--port", type=int, default=9102, help="Port to listen on")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host interface")
    args = parser.parse_args()

    uvicorn.run("src.agentic.agents.coding.server:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
