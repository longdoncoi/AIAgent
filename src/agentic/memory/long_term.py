"""Long-term Memory Store: Semantic, Episodic, and Procedural Memory with Dual-Engine (PostgreSQL & SQLite WAL) Persistence."""

import json
import logging
import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.agentic.core.config import get_settings

logger = logging.getLogger(__name__)


class MemoryEntry(BaseModel):
    id: str
    category: str  # semantic | episodic | procedural
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class LongTermStore:
    """Manages thread-safe long-term persistence across agent executions.

    Engine Support:
    1. PostgreSQL (Primary for Multi-replica Production Kubernetes clusters).
    2. SQLite WAL (Local development, edge deployments, and unit test environments).
    """

    def __init__(self, storage_dir: Path | None = None, force_sqlite: bool = False) -> None:
        self.settings = get_settings()
        self.storage_dir = storage_dir or (self.settings.project_root / "data" / "memory")
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.storage_dir / "long_term_store.db"
        self.legacy_json = self.storage_dir / "long_term_store.json"
        self._lock = threading.Lock()
        self.is_postgres = False
        self._pg_conn_str = self.settings.postgres_uri

        if not force_sqlite and (
            self.settings.use_postgres_checkpoint or self.settings.environment == "production"
        ):
            self._try_init_postgres()

        if not self.is_postgres:
            if self.settings.environment == "production" and not force_sqlite:
                raise RuntimeError(
                    "Fatal Production Configuration Error: Failed to connect to PostgreSQL LongTermStore in production. "
                    "Single-node SQLite storage is strictly prohibited across multi-replica clusters."
                )
            self._init_sqlite()

    def _try_init_postgres(self) -> None:
        """Attempts to initialize PostgreSQL database connection and apply versioned Alembic schema migrations."""
        try:
            import psycopg

            # Verify connectivity
            with psycopg.connect(self._pg_conn_str, connect_timeout=3) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1;")

            # Apply versioned migrations
            from src.agentic.memory.migrations import run_database_migrations

            run_database_migrations(self._pg_conn_str)

            self.is_postgres = True
            logger.info(
                "Initialized PostgreSQL LongTermStore backend with Alembic versioned schema."
            )
        except Exception as e:
            logger.warning(
                f"Could not connect to PostgreSQL LongTermStore ({e}). Falling back to SQLite WAL mode."
            )

    @contextmanager
    def _get_pg_conn(self) -> Generator[Any, None, None]:
        """Borrows connection from singleton psycopg pool if available, else connects directly."""
        from src.agentic.memory.short_term import get_postgres_pool

        pool = get_postgres_pool()
        if pool is not None:
            with pool.connection() as conn:
                yield conn
        else:
            import psycopg

            with psycopg.connect(self._pg_conn_str, prepare_threshold=None) as conn:
                yield conn

    @contextmanager
    def _get_sqlite_connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
        finally:
            conn.close()

    def _init_sqlite(self) -> None:
        """Initializes SQLite schema and migrates legacy JSON records if present."""
        with self._lock:
            with self._get_sqlite_connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS long_term_memories (
                        id TEXT PRIMARY KEY,
                        category TEXT NOT NULL,
                        content TEXT NOT NULL,
                        metadata TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_mem_category ON long_term_memories(category)"
                )
                conn.commit()

            if self.legacy_json.exists():
                try:
                    with open(self.legacy_json, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    with self._get_sqlite_connection() as conn:
                        for k, v in data.items():
                            meta_str = json.dumps(v.get("metadata", {}), ensure_ascii=False)
                            conn.execute(
                                """
                                INSERT OR IGNORE INTO long_term_memories (id, category, content, metadata, created_at)
                                VALUES (?, ?, ?, ?, ?)
                                """,
                                (
                                    v.get("id", k),
                                    v.get("category", "semantic"),
                                    v.get("content", ""),
                                    meta_str,
                                    v.get("created_at", datetime.now(timezone.utc).isoformat()),
                                ),
                            )
                        conn.commit()
                    logger.info("Migrated legacy JSON long-term memories to SQLite WAL store.")
                except Exception as e:
                    logger.warning(f"Error migrating legacy JSON memories: {e}")

    def add_semantic(self, key: str, fact: str, metadata: dict[str, Any] | None = None) -> None:
        """Stores a semantic fact or user preference."""
        entry_id = f"sem_{key}"
        self._upsert(entry_id, "semantic", fact, metadata or {})

    def add_episodic(
        self, task_id: str, summary: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Stores an episodic record of a completed task or decision."""
        entry_id = f"epi_{task_id}"
        self._upsert(entry_id, "episodic", summary, metadata or {})

    def add_procedural(
        self, workflow_name: str, steps_guide: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Stores a procedural workflow or recipe."""
        entry_id = f"proc_{workflow_name}"
        self._upsert(entry_id, "procedural", steps_guide, metadata or {})

    def _upsert(self, entry_id: str, category: str, content: str, metadata: dict[str, Any]) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        meta_json = json.dumps(metadata, ensure_ascii=False)

        if self.is_postgres:
            try:
                with self._get_pg_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO long_term_memories (id, category, content, metadata, created_at)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (id) DO UPDATE SET
                                content = EXCLUDED.content,
                                metadata = EXCLUDED.metadata,
                                created_at = EXCLUDED.created_at;
                            """,
                            (entry_id, category, content, meta_json, now_iso),
                        )
                    conn.commit()
                return
            except Exception as e:
                from src.agentic.observability.metrics import LTM_WRITE_ERRORS_TOTAL

                LTM_WRITE_ERRORS_TOTAL.labels(backend="postgres").inc()
                logger.error(
                    f"[DEAD-LETTER-FALLBACK] PostgreSQL upsert error for entry '{entry_id}' ({e}). "
                    "Persisting locally to SQLite WAL buffer to prevent data loss."
                )
                if self.settings.environment == "production":
                    raise RuntimeError("PostgreSQL long-term memory write failed.") from e

        with self._lock:
            with self._get_sqlite_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO long_term_memories (id, category, content, metadata, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        content = excluded.content,
                        metadata = excluded.metadata,
                        created_at = excluded.created_at
                    """,
                    (entry_id, category, content, meta_json, now_iso),
                )
                conn.commit()

    def search(self, query: str, category: str | None = None, limit: int = 5) -> list[MemoryEntry]:
        """Queries stored memories with optional category filter."""
        results: list[MemoryEntry] = []

        if self.is_postgres:
            try:
                with self._get_pg_conn() as conn:
                    with conn.cursor() as cur:
                        if category:
                            cur.execute(
                                """
                                SELECT id, category, content, metadata, created_at
                                FROM long_term_memories
                                WHERE category = %s AND content ILIKE %s
                                ORDER BY created_at DESC
                                LIMIT %s;
                                """,
                                (category, f"%{query}%", limit),
                            )
                        else:
                            cur.execute(
                                """
                                SELECT id, category, content, metadata, created_at
                                FROM long_term_memories
                                WHERE content ILIKE %s
                                ORDER BY created_at DESC
                                LIMIT %s;
                                """,
                                (f"%{query}%", limit),
                            )
                        rows = cur.fetchall()
                        for r in rows:
                            meta = r[3] if isinstance(r[3], dict) else {}
                            if isinstance(r[3], str):
                                try:
                                    meta = json.loads(r[3])
                                except Exception:
                                    pass
                            results.append(
                                MemoryEntry(
                                    id=str(r[0]),
                                    category=str(r[1]),
                                    content=str(r[2]),
                                    metadata=meta,
                                    created_at=str(r[4]),
                                )
                            )
                        return results
            except Exception as e:
                logger.error(f"PostgreSQL search error ({e}).")
                if self.settings.environment == "production":
                    raise RuntimeError("PostgreSQL long-term memory search failed.") from e
                logger.warning("Falling back to SQLite in non-production mode.")

        with self._lock:
            with self._get_sqlite_connection() as conn:
                if category:
                    cursor = conn.execute(
                        """
                        SELECT id, category, content, metadata, created_at
                        FROM long_term_memories
                        WHERE category = ? AND content LIKE ?
                        ORDER BY created_at DESC
                        LIMIT ?
                        """,
                        (category, f"%{query}%", limit),
                    )
                else:
                    cursor = conn.execute(
                        """
                        SELECT id, category, content, metadata, created_at
                        FROM long_term_memories
                        WHERE content LIKE ?
                        ORDER BY created_at DESC
                        LIMIT ?
                        """,
                        (f"%{query}%", limit),
                    )
                rows = cursor.fetchall()

        for r in rows:
            meta = {}
            row_dict: Any = r
            try:
                meta = json.loads(row_dict["metadata"])
            except Exception:
                pass
            results.append(
                MemoryEntry(
                    id=str(row_dict["id"]),
                    category=str(row_dict["category"]),
                    content=str(row_dict["content"]),
                    metadata=meta,
                    created_at=str(row_dict["created_at"]),
                )
            )
        return results


_long_term_store: LongTermStore | None = None


def get_long_term_store() -> LongTermStore:
    """Singleton getter for LongTermStore."""
    global _long_term_store
    if _long_term_store is None:
        _long_term_store = LongTermStore()
    return _long_term_store
