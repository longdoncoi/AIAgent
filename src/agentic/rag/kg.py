"""Knowledge Graph Store and Entity-Relation Extractor supporting Neo4j and NetworkX."""

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import networkx as nx
from llama_index.core.schema import NodeWithScore, TextNode

from src.agentic.core.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class EntityRelationTriple:
    """Represents a Knowledge Graph triple: Subject -> Predicate -> Object."""

    subject: str
    predicate: str
    object_: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_text(self) -> str:
        return f"{self.subject} --[{self.predicate}]--> {self.object_}"


class KnowledgeGraphStore:
    """Enterprise Knowledge Graph Store combining Neo4j with resilient in-memory NetworkX Graph."""

    def __init__(self, persist_path: Path | None = None) -> None:
        self.settings = get_settings()
        self.persist_path = persist_path or (
            self.settings.project_root / "data" / "index" / "kg_graph.json"
        )
        self.graph = nx.DiGraph()
        self._neo4j_driver: Any = None
        self._init_neo4j()
        self._load_local_graph()

    def _init_neo4j(self) -> None:
        """Attempts connection to Neo4j database."""
        try:
            import neo4j

            driver = neo4j.GraphDatabase.driver(
                self.settings.neo4j_url,
                auth=(self.settings.neo4j_user, self.settings.neo4j_password),
            )
            driver.verify_connectivity()
            self._neo4j_driver = driver
            logger.info(f"Connected to Neo4j Knowledge Graph at {self.settings.neo4j_url}")
        except Exception as e:
            logger.debug(f"Neo4j not reachable ({e}). Operating in resilient NetworkX graph mode.")
            self._neo4j_driver = None

    def _load_local_graph(self) -> None:
        """Loads serialized graph from disk if available."""
        if self.persist_path.exists():
            try:
                with open(self.persist_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for node_data in data.get("nodes", []):
                    self.graph.add_node(node_data["id"], **node_data.get("attrs", {}))
                for edge in data.get("edges", []):
                    self.graph.add_edge(
                        edge["u"], edge["v"], relation=edge.get("relation", "related_to")
                    )
                logger.info(
                    f"Loaded {self.graph.number_of_nodes()} nodes and {self.graph.number_of_edges()} edges into Knowledge Graph."
                )
            except Exception as e:
                logger.warning(f"Could not load graph from {self.persist_path}: {e}")

    def persist(self) -> None:
        """Persists in-memory graph to JSON on disk."""
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            nodes = [{"id": n, "attrs": self.graph.nodes[n]} for n in self.graph.nodes()]
            edges = [
                {"u": u, "v": v, "relation": self.graph.edges[u, v].get("relation", "related_to")}
                for u, v in self.graph.edges()
            ]
            with open(self.persist_path, "w", encoding="utf-8") as f:
                json.dump({"nodes": nodes, "edges": edges}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Failed to persist Knowledge Graph: {e}")

    def add_triple(
        self, subject: str, predicate: str, object_: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Adds an entity-relation-entity triple to both in-memory graph and Neo4j if available."""
        sub = subject.strip()
        obj = object_.strip()
        pred = predicate.strip().lower()

        if not sub or not obj:
            return

        # 1. Update in-memory NetworkX graph
        self.graph.add_node(sub, type="Entity")
        self.graph.add_node(obj, type="Entity")
        self.graph.add_edge(sub, obj, relation=pred, **(metadata or {}))

        # 2. Update Neo4j if active
        if self._neo4j_driver is not None:
            try:
                with self._neo4j_driver.session() as session:
                    session.run(
                        """
                        MERGE (s:Entity {name: $sub})
                        MERGE (o:Entity {name: $obj})
                        MERGE (s)-[r:RELATION {type: $pred}]->(o)
                        """,
                        sub=sub,
                        obj=obj,
                        pred=pred,
                    )
            except Exception as e:
                logger.debug(f"Neo4j write exception: {e}")

    def add_triples_batch(self, triples: list[EntityRelationTriple], batch_size: int = 100) -> None:
        """Batch-inserts entity-relation triples into in-memory NetworkX and Neo4j via UNWIND."""
        if not triples:
            return

        batch_rows: list[dict[str, str]] = []
        for t in triples:
            sub = t.subject.strip()
            obj = t.object_.strip()
            pred = t.predicate.strip().lower()
            if not sub or not obj:
                continue
            self.graph.add_node(sub, type="Entity")
            self.graph.add_node(obj, type="Entity")
            self.graph.add_edge(sub, obj, relation=pred, **t.metadata)
            batch_rows.append({"sub": sub, "obj": obj, "pred": pred})

        if self._neo4j_driver is not None and batch_rows:
            try:
                with self._neo4j_driver.session() as session:
                    for i in range(0, len(batch_rows), batch_size):
                        chunk = batch_rows[i : i + batch_size]
                        session.run(
                            """
                            UNWIND $batch AS row
                            MERGE (s:Entity {name: row.sub})
                            MERGE (o:Entity {name: row.obj})
                            MERGE (s)-[r:RELATION {type: row.pred}]->(o)
                            """,
                            batch=chunk,
                        )
            except Exception as e:
                logger.debug(f"Neo4j batch write exception: {e}")

    def extract_triples_from_text(self, text: str) -> list[EntityRelationTriple]:
        """Heuristic rule-based extractor for enterprise entity relations."""
        triples: list[EntityRelationTriple] = []
        lines = text.split("\n")

        # Patterns for policy/rules: X yêu cầu Y, X requires Y, X quy định Y
        rule_patterns = [
            re.compile(
                r"([^.,;\n]+)\s+(yêu cầu|requires|phải có|bắt buộc)\s+([^.,;\n]+)", re.IGNORECASE
            ),
            re.compile(
                r"([^.,;\n]+)\s+(áp dụng cho|applies to|dành cho)\s+([^.,;\n]+)", re.IGNORECASE
            ),
            re.compile(
                r"([^.,;\n]+)\s+(thuộc về|belongs to|là một phần của)\s+([^.,;\n]+)", re.IGNORECASE
            ),
            re.compile(
                r"([^.,;\n]+)\s+(có quyền|has permission|được phép)\s+([^.,;\n]+)", re.IGNORECASE
            ),
        ]

        for line in lines:
            line = line.strip()
            if not line or len(line) < 10:
                continue

            for pattern in rule_patterns:
                match = pattern.search(line)
                if match:
                    s, p, o = match.group(1).strip(), match.group(2).strip(), match.group(3).strip()
                    if 2 < len(s) < 50 and 2 < len(o) < 50:
                        triples.append(EntityRelationTriple(subject=s, predicate=p, object_=o))

        return triples

    def index_text(self, text: str, source_doc: str = "document") -> int:
        """Extracts and adds triples from text to the Knowledge Graph using batch insertion."""
        triples = self.extract_triples_from_text(text)
        for t in triples:
            t.metadata["source"] = source_doc
        self.add_triples_batch(triples)
        if triples:
            self.persist()
        return len(triples)

    async def aindex_text(self, text: str, source_doc: str = "document") -> int:
        """Asynchronously extracts and adds triples to Knowledge Graph without blocking loop."""
        import asyncio

        return await asyncio.to_thread(self.index_text, text, source_doc)

    async def aquery_graph(self, query: str, top_k: int = 5) -> list[NodeWithScore]:
        """Asynchronously queries the Knowledge Graph."""
        import asyncio

        return await asyncio.to_thread(self.query_graph, query, top_k)

    def query_graph(self, query: str, top_k: int = 5) -> list[NodeWithScore]:
        """Queries the graph for entities matching the query and returns connected knowledge subgraphs."""
        query_terms = [t.lower() for t in query.split() if len(t) > 2]
        matched_nodes: list[tuple[str, float]] = []

        for node in self.graph.nodes:
            node_lower = str(node).lower()
            score = sum(1.0 for term in query_terms if term in node_lower)
            if score > 0:
                matched_nodes.append((node, score))

        matched_nodes.sort(key=lambda x: x[1], reverse=True)
        results: list[NodeWithScore] = []

        for node_id, match_score in matched_nodes[:top_k]:
            # Collect 1-hop subgraph relationships
            out_edges = list(self.graph.out_edges(node_id, data=True))
            in_edges = list(self.graph.in_edges(node_id, data=True))

            rel_descriptions: list[str] = []
            for _, target, data in out_edges[:5]:
                rel_descriptions.append(
                    f"• {node_id} -> [{data.get('relation', 'related_to')}] -> {target}"
                )
            for source, _, data in in_edges[:5]:
                rel_descriptions.append(
                    f"• {source} -> [{data.get('relation', 'related_to')}] -> {node_id}"
                )

            if rel_descriptions:
                graph_text = f"Knowledge Graph Entity [{node_id}]:\n" + "\n".join(rel_descriptions)
                results.append(
                    NodeWithScore(
                        node=TextNode(
                            text=graph_text,
                            metadata={"type": "knowledge_graph", "entity": node_id},
                        ),
                        score=match_score,
                    )
                )

        return results


_kg_instance: KnowledgeGraphStore | None = None


def get_knowledge_graph_store() -> KnowledgeGraphStore:
    """Singleton getter for KnowledgeGraphStore."""
    global _kg_instance
    if _kg_instance is None:
        _kg_instance = KnowledgeGraphStore()
    return _kg_instance
