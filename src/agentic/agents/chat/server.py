"""A2A Server for ChatAgent (Port 9100)."""

import argparse

import uvicorn

from src.agentic.a2a.cards import CHAT_AGENT_CARD
from src.agentic.a2a.executor import create_a2a_server
from src.agentic.agents.chat.graph import get_chat_graph

graph = get_chat_graph()
app = create_a2a_server(card=CHAT_AGENT_CARD, graph=graph)


def main() -> None:
    parser = argparse.ArgumentParser(description="ChatAgent A2A Server")
    parser.add_argument("--port", type=int, default=9100, help="Port to listen on")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host interface")
    args = parser.parse_args()

    uvicorn.run("src.agentic.agents.chat.server:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
