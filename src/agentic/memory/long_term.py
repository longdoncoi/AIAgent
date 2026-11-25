"""Long-term Memory Store: Semantic, Episodic, and Procedural Memory with Dual-Engine (PostgreSQL & SQLite WAL) Persistence."""

import hashlib
import json
import logging
import re
import sqlite3
import threading
from collections.abc import Generator, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.agentic.core.config import get_settings, is_strict_env
from src.agentic.core.execution_context import get_tool_execution_context

logger = logging.getLogger(__name__)


class MemoryEntry(BaseModel):
    id: str
    category: str  # semantic | episodic | procedural
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


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

        strict = is_strict_env(self.settings)
        if not force_sqlite and (self.settings.use_postgres_checkpoint or strict):
            self._try_init_postgres()

        if not self.is_postgres:
            if strict and not force_sqlite:
                raise RuntimeError(
                    "Fatal Production Configuration Error: Failed to connect to PostgreSQL LongTermStore in production. "
                    "Single-node SQLite storage is strictly prohibited across multi-replica clusters."
                )
            self._init_sqlite()

    def _try_init_postgres(self) -> None:
        """Initializes PostgreSQL and verifies Alembic migrations (fail-closed)."""
        try:
            import psycopg

            # Verify connectivity
            with (
                psycopg.connect(self._pg_conn_str, connect_timeout=3) as conn,
                conn.cursor() as cur,
            ):
                cur.execute("SELECT 1;")

            # Verify schema without running DDL from least-privilege app roles in production.
            from src.agentic.memory.migrations import (
                check_database_migrations,
                run_database_migrations,
            )

            if is_strict_env(self.settings) and not self.settings.auto_run_migrations:
                if not check_database_migrations(self._pg_conn_str):
                    raise RuntimeError(
                        "PostgreSQL schema is not at Alembic head; run db-migration-job before startup."
                    )
            elif not run_database_migrations(self._pg_conn_str):
                raise RuntimeError("Alembic migration runner returned an unsuccessful status.")

            self.is_postgres = True
            logger.info(
                "Initialized PostgreSQL LongTermStore backend with Alembic versioned schema."
            )
        except Exception as e:
            if is_strict_env(self.settings):
                logger.error("PostgreSQL LongTermStore initialization failed in production: %s", e)
                raise RuntimeError(
                    "Fatal Production Configuration Error: PostgreSQL LongTermStore is unavailable "
                    "or its schema could not be migrated."
                ) from e
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

            with psycopg.connect(
                self._pg_conn_str, prepare_threshold=None, connect_timeout=3
            ) as conn:
                yield conn

    def _handle_pg_failure(
        self, operation: str, exc: Exception, *, entry_id: str | None = None
    ) -> None:
        """Fails closed in production; dev/test may continue with node-local SQLite."""
        context = f" (entry='{entry_id}')" if entry_id else ""
        logger.error(
            "PostgreSQL %s failed%s: %s. Node-local SQLite fallback is forbidden in production.",
            operation,
            context,
            exc,
        )
        if is_strict_env(self.settings):
            raise RuntimeError(
                f"PostgreSQL {operation} failed{context} in production; refusing node-local SQLite fallback."
            ) from exc

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
                    with open(self.legacy_json, encoding="utf-8") as f:
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
                                    v.get("created_at", datetime.now(UTC).isoformat()),
                                ),
                            )
                        conn.commit()
                    logger.info("Migrated legacy JSON long-term memories to SQLite WAL store.")
                except Exception as e:
                    logger.warning(f"Error migrating legacy JSON memories: {e}")

    def _prepare_owned_metadata(self, metadata: dict[str, Any] | None) -> dict[str, Any]:
        """Resolve and validate the authoritative owner for a memory record."""
        owned = dict(metadata or {})
        context = get_tool_execution_context()
        contextual_owner = context.owner_id if context is not None else None
        supplied_owner = owned.get("user_id")
        if supplied_owner and contextual_owner and str(supplied_owner) != str(contextual_owner):
            raise PermissionError("Long-term memory owner does not match execution context.")
        owner_id = str(supplied_owner or contextual_owner or "")
        if not owner_id:
            if is_strict_env(self.settings):
                raise PermissionError("Production long-term memory writes require an owner_id.")
            owner_id = "local_system"
        _validate_user_id(owner_id)
        owned["user_id"] = owner_id
        return owned

    @staticmethod
    def _owned_entry_id(category: str, logical_key: str, owner_id: str) -> str:
        """Create an opaque, tenant-namespaced primary key.

        Hashing avoids leaking principal identifiers through database primary
        keys while guaranteeing that two tenants can never overwrite one
        another's semantic/episodic/procedural record.
        """
        digest = hashlib.sha256(
            f"{category}\x00{owner_id}\x00{logical_key}".encode()
        ).hexdigest()
        return f"{category[:4]}_{digest}"

    def add_semantic(self, key: str, fact: str, metadata: dict[str, Any] | None = None) -> None:
        """Stores a semantic fact or user preference."""
        owned = self._prepare_owned_metadata(metadata)
        owned.setdefault("memory_key", key)
        entry_id = self._owned_entry_id("semantic", key, str(owned["user_id"]))
        self._upsert(entry_id, "semantic", fact, owned)

    def add_episodic(
        self, task_id: str, summary: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Stores an episodic record of a completed task or decision."""
        owned = self._prepare_owned_metadata(metadata)
        owned.setdefault("task_id", task_id)
        entry_id = self._owned_entry_id("episodic", task_id, str(owned["user_id"]))
        self._upsert(entry_id, "episodic", summary, owned)

    def add_procedural(
        self, workflow_name: str, steps_guide: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Stores a procedural workflow or recipe."""
        owned = self._prepare_owned_metadata(metadata)
        owned.setdefault("workflow_name", workflow_name)
        entry_id = self._owned_entry_id("procedural", workflow_name, str(owned["user_id"]))
        self._upsert(entry_id, "procedural", steps_guide, owned)

    def _upsert(self, entry_id: str, category: str, content: str, metadata: dict[str, Any]) -> None:
        now_iso = datetime.now(UTC).isoformat()
        metadata = dict(metadata)
        context = get_tool_execution_context()
        if (
            context is not None
            and context.owner_id
            and metadata.get("user_id") != context.owner_id
        ):
            raise PermissionError("Long-term memory owner changed after authorization.")
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
                self._handle_pg_failure("upsert", e, entry_id=entry_id)

        with self._lock, self._get_sqlite_connection() as conn:
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

    def search(
        self,
        query: str,
        category: str | None = None,
        limit: int = 5,
        user_id: str | None = None,
    ) -> list[MemoryEntry]:
        """Queries stored memories with optional category and strict user filter."""
        results: list[MemoryEntry] = []
        context = get_tool_execution_context()
        context_owner = context.owner_id if context is not None else None
        if user_id and context_owner and user_id != context_owner:
            raise PermissionError("Cross-tenant long-term memory search is forbidden.")
        effective_user_id = user_id or context_owner
        if is_strict_env(self.settings) and not effective_user_id:
            raise PermissionError("Production long-term memory searches require an owner_id.")

        if self.is_postgres:
            try:
                pg_queries = {
                    (False, False): (
                        "SELECT id, category, content, metadata, created_at FROM long_term_memories "
                        "WHERE (to_tsvector('simple', content) @@ plainto_tsquery('simple', %s) OR content ILIKE %s) "
                        "ORDER BY ts_rank(to_tsvector('simple', content), plainto_tsquery('simple', %s)) DESC, created_at DESC LIMIT %s;"
                    ),
                    (True, False): (
                        "SELECT id, category, content, metadata, created_at FROM long_term_memories "
                        "WHERE (to_tsvector('simple', content) @@ plainto_tsquery('simple', %s) OR content ILIKE %s) "
                        "AND category = %s "
                        "ORDER BY ts_rank(to_tsvector('simple', content), plainto_tsquery('simple', %s)) DESC, created_at DESC LIMIT %s;"
                    ),
                    (False, True): (
                        "SELECT id, category, content, metadata, created_at FROM long_term_memories "
                        "WHERE (to_tsvector('simple', content) @@ plainto_tsquery('simple', %s) OR content ILIKE %s) "
                        "AND metadata @> %s::jsonb "
                        "ORDER BY ts_rank(to_tsvector('simple', content), plainto_tsquery('simple', %s)) DESC, created_at DESC LIMIT %s;"
                    ),
                    (True, True): (
                        "SELECT id, category, content, metadata, created_at FROM long_term_memories "
                        "WHERE (to_tsvector('simple', content) @@ plainto_tsquery('simple', %s) OR content ILIKE %s) "
                        "AND category = %s AND metadata @> %s::jsonb "
                        "ORDER BY ts_rank(to_tsvector('simple', content), plainto_tsquery('simple', %s)) DESC, created_at DESC LIMIT %s;"
                    ),
                }
                with self._get_pg_conn() as conn, conn.cursor() as cur:
                    params: list[Any] = [query, f"%{query}%"]
                    if category:
                        params.append(category)
                    if effective_user_id:
                        # JSONB containment keeps the user filter strict and
                        # independent of the (caller-controlled) query text.
                        params.append(json.dumps({"user_id": effective_user_id}))
                    cur.execute(
                        pg_queries[(bool(category), bool(effective_user_id))],
                        (*params, query, limit),
                    )
                    rows = cur.fetchall()
                    for r in rows:
                        meta = r[3] if isinstance(r[3], dict) else {}
                        if isinstance(r[3], str):
                            with suppress(Exception):
                                meta = json.loads(r[3])
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
                self._handle_pg_failure("search", e)

        sqlite_queries = {
            (False, False): (
                "SELECT id, category, content, metadata, created_at FROM long_term_memories "
                "ORDER BY created_at DESC LIMIT ?"
            ),
            (True, False): (
                "SELECT id, category, content, metadata, created_at FROM long_term_memories "
                "WHERE category = ? ORDER BY created_at DESC LIMIT ?"
            ),
            (False, True): (
                "SELECT id, category, content, metadata, created_at FROM long_term_memories "
                "WHERE json_extract(metadata, '$.user_id') = ? "
                "ORDER BY created_at DESC LIMIT ?"
            ),
            (True, True): (
                "SELECT id, category, content, metadata, created_at FROM long_term_memories "
                "WHERE category = ? AND json_extract(metadata, '$.user_id') = ? "
                "ORDER BY created_at DESC LIMIT ?"
            ),
        }
        with self._lock, self._get_sqlite_connection() as conn:
            sqlite_params: list[Any] = []
            if category:
                sqlite_params.append(category)
            if effective_user_id:
                sqlite_params.append(effective_user_id)
            cursor = conn.execute(
                sqlite_queries[(bool(category), bool(effective_user_id))],
                (*sqlite_params, min(500, max(50, limit * 20))),
            )
            rows = cursor.fetchall()

        terms = {
            term
            for term in re.findall(r"\w+", query.casefold())
            if len(term) >= 3
        }
        ranked_rows = sorted(
            rows,
            key=lambda row: sum(str(row["content"]).casefold().count(term) for term in terms),
            reverse=True,
        )
        matched_rows = [
            row for row in ranked_rows
            if not terms or any(term in str(row["content"]).casefold() for term in terms)
        ][:limit]
        for r in matched_rows:
            meta = {}
            row_dict: Any = r
            with suppress(Exception):
                meta = json.loads(row_dict["metadata"])
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

    def _entries_from_rows(self, rows: Sequence[Any], *, postgres: bool) -> list[MemoryEntry]:
        """Map DB rows (tuples or sqlite.Row) to MemoryEntry objects."""
        entries: list[MemoryEntry] = []
        for row in rows:
            meta: Any = row[3] if postgres else row["metadata"]
            if not isinstance(meta, dict):
                try:
                    meta = json.loads(str(meta))
                except Exception:
                    meta = {}
            entries.append(
                MemoryEntry(
                    id=str(row[0] if postgres else row["id"]),
                    category=str(row[1] if postgres else row["category"]),
                    content=str(row[2] if postgres else row["content"]),
                    metadata=meta,
                    created_at=str(row[4] if postgres else row["created_at"]),
                )
            )
        return entries

    def list_by_user(self, user_id: str) -> list[MemoryEntry]:
        """Enumerate every memory record belonging to ``user_id``.

        Used by the Right-to-be-Forgotten purge to know exactly what will be
        erased. The filter is applied in SQL via a JSON containment predicate;
        no user-controlled string is interpolated into the statement.
        """
        _validate_user_id(user_id)
        if self.is_postgres:
            try:
                with self._get_pg_conn() as conn, conn.cursor() as cur:
                    cur.execute(
                        """SELECT id, category, content, metadata, created_at
                               FROM long_term_memories
                               WHERE metadata @> %s::jsonb
                               ORDER BY created_at DESC;""",
                        (json.dumps({"user_id": user_id}),),
                    )
                    return self._entries_from_rows(cur.fetchall(), postgres=True)
            except Exception as e:
                self._handle_pg_failure("list_by_user", e)

        with self._lock, self._get_sqlite_connection() as conn:
            cursor = conn.execute(
                """SELECT id, category, content, metadata, created_at
                       FROM long_term_memories
                       WHERE json_extract(metadata, '$.user_id') = ?
                       ORDER BY created_at DESC;""",
                (user_id,),
            )
            return self._entries_from_rows(cursor.fetchall(), postgres=False)

    def delete_by_user(self, user_id: str) -> int:
        """Delete all memory records for ``user_id`` and return the row count."""
        _validate_user_id(user_id)
        if self.is_postgres:
            try:
                with self._get_pg_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """DELETE FROM long_term_memories
                               WHERE metadata @> %s::jsonb;""",
                            (json.dumps({"user_id": user_id}),),
                        )
                        deleted = int(cur.rowcount or 0)
                    conn.commit()
                    return deleted
            except Exception as e:
                self._handle_pg_failure("delete_by_user", e)

        with self._lock, self._get_sqlite_connection() as conn:
            cursor = conn.execute(
                """DELETE FROM long_term_memories
                       WHERE json_extract(metadata, '$.user_id') = ?;""",
                (user_id,),
            )
            deleted = int(cursor.rowcount or 0)
            conn.commit()
            return deleted

    def delete_older_than(self, max_age_days: int = 90) -> int:
        """Delete long-term memory entries older than ``max_age_days`` (retention policy)."""
        from datetime import timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=max(1, max_age_days))).isoformat()
        if self.is_postgres:
            try:
                with self._get_pg_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM long_term_memories WHERE created_at < %s;",
                            (cutoff,),
                        )
                        deleted = int(cur.rowcount or 0)
                    conn.commit()
                    return deleted
            except Exception as e:
                self._handle_pg_failure("delete_older_than", e)

        with self._lock, self._get_sqlite_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM long_term_memories WHERE created_at < ?;",
                (cutoff,),
            )
            deleted = int(cursor.rowcount or 0)
            conn.commit()
            return deleted


_USER_ID_RE = re.compile(r"^[A-Za-z0-9_.:@\-]{1,255}$")


def _validate_user_id(user_id: str) -> None:
    if not isinstance(user_id, str) or not _USER_ID_RE.fullmatch(user_id):
        raise ValueError("Invalid user_id.")


_long_term_store: LongTermStore | None = None


def get_long_term_store() -> LongTermStore:
    """Singleton getter for LongTermStore."""
    global _long_term_store
    if _long_term_store is None:
        _long_term_store = LongTermStore()
    return _long_term_store
