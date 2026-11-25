"""Data-lifecycle operations for privacy compliance (Right To Be Forgotten).

The purge service deliberately fans out to *every* store that can hold
personal data for a principal:

1. PostgreSQL LangGraph checkpoint tables (``checkpoints``,
   ``checkpoint_blobs``, ``checkpoint_writes``).
2. Redis checkpoint / writes / index / thread-owner keys (SCAN only, never
   ``KEYS``).
3. Long-term memory (semantic / episodic / procedural records).
4. RAG cache entries (best-effort, via the public invalidation API).

Every SQL statement is fully parameterised; all Redis glob patterns are built
from validated, escaped identifiers. The operation is idempotent: a second
purge reports zero deletions.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from src.agentic.core.config import get_settings, is_strict_env
from src.agentic.core.execution_context import cost_allocation_tags
from src.agentic.memory.long_term import MemoryEntry, get_long_term_store
from src.agentic.memory.short_term import get_postgres_pool
from src.agentic.memory.thread_registry import get_thread_registry
from src.agentic.safety.audit import record_audit_event

logger = logging.getLogger(__name__)
_AUDIT_LOGGER = logging.getLogger("agentic.audit.data_lifecycle")

# Hardcoded table names keep the SQL text free of any caller-controlled data.
# Every value is supplied through DB-API parameters. Ordered child-before-parent
# so foreign-key or dependent rows are removed cleanly before parent checkpoints.
_CHECKPOINT_SQL: dict[str, tuple[str, str]] = {
    "checkpoint_writes": (
        "DELETE FROM checkpoint_writes WHERE thread_id = %s",
        "SELECT COUNT(*) FROM checkpoint_writes WHERE thread_id = %s",
    ),
    "checkpoint_blobs": (
        "DELETE FROM checkpoint_blobs WHERE thread_id = %s",
        "SELECT COUNT(*) FROM checkpoint_blobs WHERE thread_id = %s",
    ),
    "checkpoints": (
        "DELETE FROM checkpoints WHERE thread_id = %s",
        "SELECT COUNT(*) FROM checkpoints WHERE thread_id = %s",
    ),
}
_THREAD_ENUMERATION_SQL = (
    "SELECT DISTINCT thread_id FROM checkpoints "
    "WHERE metadata->>'user_id' = %s OR metadata->>'owner_id' = %s"
)
_STALE_THREAD_SUBQUERY = (
    "SELECT thread_id FROM checkpoints GROUP BY thread_id "
    "HAVING MAX(created_at) < NOW() - (%s * INTERVAL '1 second')"
)
_COUNT_STALE_THREADS_SQL = (
    "SELECT COUNT(*) FROM ("
    "SELECT thread_id FROM checkpoints GROUP BY thread_id "
    "HAVING MAX(created_at) < NOW() - (%s * INTERVAL '1 second')"
    ") AS stale_threads"
)
_DELETE_STALE_SQL: dict[str, str] = {
    "checkpoint_writes": (
        "DELETE FROM checkpoint_writes WHERE thread_id IN ("
        "SELECT thread_id FROM checkpoints GROUP BY thread_id "
        "HAVING MAX(created_at) < NOW() - (%s * INTERVAL '1 second')"
        ")"
    ),
    "checkpoint_blobs": (
        "DELETE FROM checkpoint_blobs WHERE thread_id IN ("
        "SELECT thread_id FROM checkpoints GROUP BY thread_id "
        "HAVING MAX(created_at) < NOW() - (%s * INTERVAL '1 second')"
        ")"
    ),
    "checkpoints": (
        "DELETE FROM checkpoints WHERE thread_id IN ("
        "SELECT thread_id FROM checkpoints GROUP BY thread_id "
        "HAVING MAX(created_at) < NOW() - (%s * INTERVAL '1 second')"
        ")"
    ),
}

_CHECKPOINT_KEY_PREFIX = "agentic:checkpoint"
_THREAD_OWNER_PREFIX = "agentic:thread-owner:"
_OWNER_THREADS_PREFIX = "agentic:owner-threads:"
_GLOB_SPECIAL = re.compile(r"([*?\[\]{}])")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:@\-]{1,255}$")


class PurgePermissionError(PermissionError):
    """Raised when the requested thread is owned by a different principal."""


class PurgeBackendUnavailable(RuntimeError):
    """Raised when ownership cannot be proven before destructive deletion."""


@dataclass
class PurgeReport:
    """Structured, auditable result of a ``purge_user_data`` invocation."""

    user_id: str
    thread_id: str | None = None
    dry_run: bool = False
    checkpoint_thread_ids: list[str] = field(default_factory=list)
    postgres_rows_deleted: dict[str, int] = field(default_factory=dict)
    redis_keys_deleted: int = 0
    redis_keys: list[str] = field(default_factory=list)
    long_term_entries_deleted: int = 0
    long_term_entry_ids: list[str] = field(default_factory=list)
    rag_cache_entries_invalidated: int = 0
    qdrant_points_deleted: int = 0
    knowledge_graph_nodes_deleted: int = 0
    raw_files_deleted: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        """``partial`` on error, ``dry_run`` for previews, otherwise ``ok``."""
        if self.errors:
            return "partial"
        if self.dry_run:
            return "dry_run"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation for the API/CLI."""
        return {
            "user_id": self.user_id,
            "thread_id": self.thread_id,
            "status": self.status,
            "dry_run": self.dry_run,
            "checkpoint_thread_ids": list(self.checkpoint_thread_ids),
            "postgres_rows_deleted": dict(self.postgres_rows_deleted),
            "redis_keys_deleted": self.redis_keys_deleted,
            "redis_keys": list(self.redis_keys),
            "long_term_entries_deleted": self.long_term_entries_deleted,
            "long_term_entry_ids": list(self.long_term_entry_ids),
            "rag_cache_entries_invalidated": self.rag_cache_entries_invalidated,
            "qdrant_points_deleted": self.qdrant_points_deleted,
            "knowledge_graph_nodes_deleted": self.knowledge_graph_nodes_deleted,
            "raw_files_deleted": self.raw_files_deleted,
            "errors": list(self.errors),
        }


def _validate_identifier(value: str, label: str) -> str:
    """Reject empty/malformed identifiers before they reach any backend."""
    if not isinstance(value, str) or not value or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"Invalid {label}: must be 1-255 safe identifier characters.")
    return value


def _escape_redis_glob(value: str) -> str:
    """Escape glob metacharacters so a thread id cannot widen a SCAN pattern."""
    return _GLOB_SPECIAL.sub(r"\\\1", value)


def cleanup_stale_postgres_checkpoints(
    max_idle_seconds: int,
    *,
    pool: Any | None = None,
    dry_run: bool = False,
) -> int:
    """Delete all checkpoint rows for threads inactive beyond the retention window.

    The stale set is derived exclusively from the server-managed ``created_at``
    column. Table names and SQL are static; only the bounded retention value is
    parameterised. If PostgreSQL is unavailable the operation fails closed.
    """
    if not 3600 <= max_idle_seconds <= 86400 * 365:
        raise ValueError("max_idle_seconds must be between 3600 and 31536000")
    active_pool = pool or get_postgres_pool()
    if active_pool is None:
        raise PurgeBackendUnavailable("PostgreSQL checkpoint backend is unavailable")

    with active_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(_COUNT_STALE_THREADS_SQL, (max_idle_seconds,))
        row = cur.fetchone()
        stale_threads = int(row[0] if row else 0)
        if dry_run or stale_threads == 0:
            return stale_threads

        # Dependants first. LangGraph's checkpoint tables intentionally do not
        # rely on cascade constraints across every supported schema version.
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            cur.execute(_DELETE_STALE_SQL[table], (max_idle_seconds,))
    _AUDIT_LOGGER.info(
        "CHECKPOINT_RETENTION_PURGE stale_threads=%s max_idle_seconds=%s",
        stale_threads,
        max_idle_seconds,
    )
    record_audit_event(
        event_type="checkpoint_retention",
        actor_id="system",
        decision="ok",
        details={
            "stale_threads": stale_threads,
            "max_idle_seconds": max_idle_seconds,
        },
    )
    return stale_threads


@contextmanager
def _pg_connection(provided: Any | None) -> Iterator[Any | None]:
    """Yield an injected connection, a pooled connection, or a direct one."""
    if provided is not None:
        yield provided
        return
    pool = get_postgres_pool()
    if pool is not None:
        with pool.connection() as conn:
            yield conn
        return
    try:
        import psycopg

        with psycopg.connect(get_settings().postgres_uri, connect_timeout=3) as conn:
            yield conn
    except Exception as exc:  # pragma: no cover - depends on environment
        logger.warning("Purge could not open a PostgreSQL connection: %s", exc)
        yield None


def _pg_is_expected() -> bool:
    settings = get_settings()
    return bool(settings.use_postgres_checkpoint or is_strict_env(settings))


def _postgres_thread_ids(conn: Any, user_id: str) -> set[str]:
    """Enumerate checkpoint threads owned by the user using bound parameters."""
    with conn.cursor() as cur:
        cur.execute(_THREAD_ENUMERATION_SQL, (user_id, user_id))
        rows = cur.fetchall()
    return {str(row[0]) for row in rows if row and row[0]}


def _purge_postgres(
    conn: Any,
    thread_ids: set[str],
    report: PurgeReport,
    *,
    dry_run: bool,
) -> None:
    """Delete (or count in dry-run mode) checkpoint rows for the target threads."""
    with conn.cursor() as cur:
        for table, (delete_sql, count_sql) in _CHECKPOINT_SQL.items():
            deleted = 0
            for thread_id in sorted(thread_ids):
                if dry_run:
                    cur.execute(count_sql, (thread_id,))
                    row = cur.fetchone()
                    deleted += int(row[0]) if row else 0
                else:
                    cur.execute(delete_sql, (thread_id,))
                    deleted += int(cur.rowcount or 0)
            report.postgres_rows_deleted[table] = deleted
        if not dry_run:
            conn.commit()


def _purge_postgres_for_user(
    user_id: str,
    target_threads: set[str],
    report: PurgeReport,
    *,
    pg_conn: Any | None,
    dry_run: bool,
) -> set[str]:
    """Run the complete synchronous PostgreSQL purge in a worker thread."""
    with _pg_connection(pg_conn) as conn:
        if conn is None:
            raise RuntimeError("PostgreSQL connection is unavailable.")
        enum_targets = _postgres_thread_ids(conn, user_id)
        target_threads.update(enum_targets)
        _purge_postgres(conn, target_threads, report, dry_run=dry_run)
    return target_threads


def _redis_patterns(
    target_threads: set[str],
    public_thread_id: str | None,
    *,
    owner_id: str | None = None,
) -> list[str]:
    patterns: list[str] = []
    public_ids: set[str] = set()
    for thread_id in sorted(target_threads):
        escaped = _escape_redis_glob(thread_id)
        # Cover both the legacy flat layout and the tenant-namespaced layout
        # emitted by RedisSaver. The thread id is itself owner-scoped (HMAC),
        # so the wildcard tenant component cannot match another principal.
        patterns.extend(
            (
                f"{_CHECKPOINT_KEY_PREFIX}:{escaped}:*",
                f"{_CHECKPOINT_KEY_PREFIX}:writes:{escaped}:*",
                f"{_CHECKPOINT_KEY_PREFIX}:index:{escaped}",
                f"{_CHECKPOINT_KEY_PREFIX}:index:{escaped}:*",
                f"{_CHECKPOINT_KEY_PREFIX}:tenant:*:{escaped}:*",
                f"{_CHECKPOINT_KEY_PREFIX}:writes:tenant:*:{escaped}:*",
                # Index keys have no trailing checkpoint id.
                f"{_CHECKPOINT_KEY_PREFIX}:index:tenant:*:{escaped}",
                f"{_CHECKPOINT_KEY_PREFIX}:index:tenant:*:{escaped}:*",
            )
        )
        if not thread_id.startswith("thread:"):
            public_ids.add(thread_id)
    if public_thread_id:
        public_ids.add(public_thread_id)
    for pub_id in sorted(public_ids):
        digest = hashlib.sha256(pub_id.encode("utf-8")).hexdigest()
        patterns.append(f"{_THREAD_OWNER_PREFIX}{digest}")
    if owner_id:
        owner_digest = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()
        patterns.append(f"{_OWNER_THREADS_PREFIX}{owner_digest}")
    return patterns


def _purge_redis(
    client: Any,
    patterns: Sequence[str],
    report: PurgeReport,
    *,
    dry_run: bool,
) -> None:
    """SCAN explicit namespace patterns and delete only matching keys."""
    seen: set[str] = set()
    for pattern in patterns:
        for raw_key in client.scan_iter(match=pattern, count=200):
            key = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else str(raw_key)
            if key in seen:
                continue
            seen.add(key)
            if not dry_run:
                # Delete the *original* key object: Redis checkpoint keys may be
                # binary bytes and must not be re-encoded before deletion.
                client.delete(raw_key)
            report.redis_keys.append(key)
    report.redis_keys_deleted = len(report.redis_keys)


def _default_redis_client() -> Any | None:
    from src.agentic.core.redis import get_redis_client

    settings = get_settings()
    # Binary-safe client targeting authoritative state Redis (falling back to cache Redis).
    target_url = settings.redis_state_url or settings.redis_url
    return get_redis_client(target_url, decode_responses=False)


def _default_cache_redis_client() -> Any | None:
    from src.agentic.core.redis import get_redis_client

    settings = get_settings()
    if settings.redis_state_url and settings.redis_url and settings.redis_state_url != settings.redis_url:
        return get_redis_client(settings.redis_url, decode_responses=False)
    return None


def _purge_qdrant_partition(
    *,
    settings: Any,
    user_id: str,
    tenant_id: str | None,
    purge_tenant_corpus: bool,
    dry_run: bool,
    qdrant_client: Any | None,
) -> int:
    """Count and optionally delete a Qdrant partition synchronously."""
    from qdrant_client import QdrantClient, models

    client = qdrant_client or QdrantClient(
        host=settings.qdrant_host,
        port=settings.qdrant_port,
        api_key=settings.qdrant_api_key,
        timeout=5,
    )
    if purge_tenant_corpus and tenant_id is not None:
        match_key = "tenant_id"
        match_value = tenant_id
    else:
        match_key = "owner_id"
        match_value = user_id
    qdrant_filter = models.Filter(
        must=[
            models.FieldCondition(
                key=match_key,
                match=models.MatchValue(value=match_value),
            )
        ]
    )
    count_result = client.count(
        collection_name=settings.qdrant_collection_name,
        count_filter=qdrant_filter,
        exact=True,
    )
    count = int(getattr(count_result, "count", 0))
    if not dry_run and count:
        client.delete(
            collection_name=settings.qdrant_collection_name,
            points_selector=models.FilterSelector(filter=qdrant_filter),
            wait=True,
        )
    return count


def _purge_tenant_raw_files(project_root: Any, tenant_id: str, *, dry_run: bool) -> int:
    """Count and optionally remove a tenant raw-data tree synchronously."""
    tenant_root = (project_root / "data" / "raw" / "tenants").resolve()
    tenant_dir = (tenant_root / tenant_id).resolve()
    if tenant_dir.parent != tenant_root:
        raise RuntimeError("Unsafe tenant raw-data path.")
    if not tenant_dir.exists():
        return 0
    count = sum(1 for item in tenant_dir.rglob("*") if item.is_file())
    if not dry_run:
        shutil.rmtree(tenant_dir)
    return count


def _add_scoped_candidates(targets: set[str], registry: Any, public_id: str, owner_id: str) -> None:
    candidate_fn = getattr(registry, "candidate_scoped_thread_ids", None)
    if callable(candidate_fn):
        for cid in candidate_fn(public_id, owner_id):
            targets.add(str(cid))
    else:
        targets.add(str(registry.scoped_thread_id(public_id, owner_id)))


async def _resolve_target_threads(
    user_id: str,
    thread_id: str | None,
    entries: Sequence[MemoryEntry],
) -> tuple[set[str], str | None]:
    """Resolve checkpoint thread ids owned by ``user_id`` from every source."""
    registry = get_thread_registry()
    targets: set[str] = set()

    if thread_id is not None:
        validated_thread_id = _validate_identifier(thread_id, "thread_id")
        public_thread_id: str | None = validated_thread_id
        try:
            owner = await registry.owner(validated_thread_id)
        except Exception as exc:  # registry outage must not delete foreign data
            logger.warning("Could not verify thread owner during purge: %s", exc)
            raise PurgeBackendUnavailable(
                "Thread ownership registry is unavailable; purge refused."
            ) from exc
        if owner is None:
            raise PurgePermissionError("Thread ownership is unclaimed; purge refused.")
        if str(owner) != user_id:
            raise PurgePermissionError("Thread is owned by another principal.")
        _add_scoped_candidates(targets, registry, validated_thread_id, user_id)
        # Some legacy checkpoints were written with the unscoped public id.
        targets.add(validated_thread_id)
    else:
        public_thread_id = None
        # Enumerate every thread owned by this principal so a user-wide purge
        # does not depend on checkpoint metadata being present.
        try:
            owned = await registry.threads_for_owner(user_id)
        except Exception as exc:
            logger.warning("Could not enumerate owned threads for purge: %s", exc)
            raise PurgeBackendUnavailable(
                "Thread ownership registry is unavailable; purge refused."
            ) from exc
        for public_id in owned:
            if public_id:
                _add_scoped_candidates(targets, registry, public_id, user_id)
                targets.add(public_id)

    for entry in entries:
        metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
        for key in ("public_thread_id", "thread_id"):
            candidate = metadata.get(key)
            if isinstance(candidate, str) and candidate:
                _add_scoped_candidates(targets, registry, candidate, user_id)
                targets.add(candidate)
    return {target for target in targets if target}, public_thread_id


async def purge_user_data(
    user_id: str,
    thread_id: str | None = None,
    *,
    actor_id: str | None = None,
    tenant_id: str | None = None,
    purge_tenant_corpus: bool = False,
    dry_run: bool = False,
    pg_conn: Any | None = None,
    redis_client: Any | None = None,
    long_term_store: Any | None = None,
    rag_cache: Any | None = None,
    qdrant_client: Any | None = None,
    knowledge_graph_store: Any | None = None,
) -> PurgeReport:
    """Delete every persistable trace of ``user_id`` (optionally one thread).

    Args:
        user_id: Principal whose data must be erased.
        thread_id: Optional *public* thread id to narrow the purge.
        tenant_id: Optional explicit tenant identifier for org-level corpus purge.
        purge_tenant_corpus: When True (with explicit ``tenant_id``), delete
            the whole tenant RAG/KG partition. User-level RTBF (False) only
            deletes user-owned vectors (``owner_id == user_id``) so an individual
            user erasure never wipes shared organizational documents.
        dry_run: When true, compute the report without mutating any store.
        pg_conn: Optional injected PostgreSQL connection (testing/CLI).
        redis_client: Optional injected Redis client (testing/CLI).
        long_term_store: Optional injected ``LongTermStore``.
        rag_cache: Optional injected RAG cache exposing ``invalidate_by_source``.

    Returns:
        A :class:`PurgeReport` describing exactly what was (or would be) erased.
        Re-running the same purge is safe and reports zero deletions.
    """
    _validate_identifier(user_id, "user_id")
    if tenant_id is not None:
        _validate_identifier(tenant_id, "tenant_id")
    if purge_tenant_corpus and not tenant_id:
        raise ValueError("Explicit tenant_id is required when purge_tenant_corpus=True.")
    report = PurgeReport(user_id=user_id, thread_id=thread_id, dry_run=dry_run)
    store = long_term_store if long_term_store is not None else get_long_term_store()

    # Fetch the user's long-term entries once: they are used both to discover
    # thread ids and to report what was erased.
    entries: list[MemoryEntry] = []
    try:
        entries = list(await asyncio.to_thread(store.list_by_user, user_id))
    except Exception as exc:
        report.errors.append(f"long_term_list: {exc}")
        logger.exception("Could not enumerate long-term memory for purge.")

    try:
        target_threads, public_thread_id = await _resolve_target_threads(user_id, thread_id, entries)
    except (PurgePermissionError, PurgeBackendUnavailable):
        raise
    except Exception as exc:
        report.errors.append(f"thread_resolution: {exc}")
        logger.exception("Could not resolve thread ids for purge.")
        target_threads, public_thread_id = set(), thread_id

    # 1. PostgreSQL LangGraph checkpoints -------------------------------------
    postgres_expected = _pg_is_expected() or pg_conn is not None
    if postgres_expected:
        try:
            target_threads = await asyncio.to_thread(
                _purge_postgres_for_user,
                user_id,
                target_threads,
                report,
                pg_conn=pg_conn,
                dry_run=dry_run,
            )
        except Exception as exc:
            report.errors.append(f"postgres: {exc}")
            logger.exception("Data purge failed for PostgreSQL store.")
    report.checkpoint_thread_ids = sorted(target_threads)

    # 2. Redis checkpoint & thread-ownership keys -----------------------------
    clients: list[Any] = []
    primary_redis = (
        redis_client
        if redis_client is not None
        else await asyncio.to_thread(_default_redis_client)
    )
    if primary_redis is not None:
        clients.append(primary_redis)
    if redis_client is None:
        secondary_cache_redis = await asyncio.to_thread(_default_cache_redis_client)
        if secondary_cache_redis is not None and secondary_cache_redis is not primary_redis:
            clients.append(secondary_cache_redis)

    patterns = _redis_patterns(
        target_threads,
        public_thread_id,
        owner_id=user_id if thread_id is None else None,
    )
    for client in clients:
        try:
            await asyncio.to_thread(
                _purge_redis,
                client,
                patterns,
                report,
                dry_run=dry_run,
            )
            if not dry_run and public_thread_id and hasattr(client, "srem"):
                owner_digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
                await asyncio.to_thread(
                    client.srem,
                    f"{_OWNER_THREADS_PREFIX}{owner_digest}",
                    public_thread_id,
                )
        except Exception as exc:
            report.errors.append(f"redis: {exc}")
            logger.exception("Data purge failed for Redis store.")

    if not dry_run:
        import inspect

        registry = get_thread_registry()
        forget_fn = getattr(registry, "forget_local", None)
        if callable(forget_fn):
            public_ids = {t for t in target_threads if not t.startswith("thread:")}
            if public_thread_id:
                public_ids.add(public_thread_id)
            res = forget_fn(
                owner_id=user_id if thread_id is None else None,
                thread_ids=public_ids or None,
            )
            if inspect.isawaitable(res):
                await res

    # 3. Long-term memory -----------------------------------------------------
    try:
        report.long_term_entry_ids = [entry.id for entry in entries]
        if dry_run:
            report.long_term_entries_deleted = len(entries)
        else:
            report.long_term_entries_deleted = int(
                await asyncio.to_thread(store.delete_by_user, user_id)
            )
    except Exception as exc:
        report.errors.append(f"long_term: {exc}")
        logger.exception("Data purge failed for LongTermStore.")

    # 4. RAG cache (best effort, public API only) -----------------------------
    if not dry_run:
        try:
            cache = rag_cache if rag_cache is not None else get_rag_cache()
            report.rag_cache_entries_invalidated = int(
                await asyncio.to_thread(cache.invalidate_by_source, user_id) or 0
            )
        except Exception as exc:
            report.errors.append(f"rag_cache: {exc}")
            logger.warning("RAG cache purge was best-effort and failed: %s", exc)

    # 5. User-owned / explicit tenant-scoped RAG vectors & knowledge graph ----
    # A thread-only purge must not erase documents shared by the user's other
    # threads. User RTBF only deletes user-owned points (owner_id == user_id);
    # org-wide tenant corpus deletion requires explicit purge_tenant_corpus=True.
    if thread_id is None:
        settings = get_settings()
        if settings.enable_qdrant or qdrant_client is not None:
            try:
                report.qdrant_points_deleted = await asyncio.to_thread(
                    _purge_qdrant_partition,
                    settings=settings,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    purge_tenant_corpus=purge_tenant_corpus,
                    dry_run=dry_run,
                    qdrant_client=qdrant_client,
                )
            except Exception as exc:
                report.errors.append(f"qdrant: {exc}")
                logger.exception("Data purge failed for Qdrant partition.")

        if purge_tenant_corpus and tenant_id is not None:
            if settings.enable_knowledge_graph or knowledge_graph_store is not None:
                try:
                    if knowledge_graph_store is None:
                        raise PurgeBackendUnavailable(
                            "Knowledge-graph store must be injected by the calling layer."
                        )
                    operation = (
                        knowledge_graph_store.count_tenant
                        if dry_run
                        else knowledge_graph_store.delete_tenant
                    )
                    report.knowledge_graph_nodes_deleted = int(
                        await asyncio.to_thread(operation, tenant_id)
                    )
                except Exception as exc:
                    report.errors.append(f"knowledge_graph: {exc}")
                    logger.exception("Data purge failed for knowledge graph tenant partition.")

            # Tenant-owned raw uploads are confined below data/raw/tenants. Resolve
            # both paths before recursive deletion to prove the target cannot escape.
            try:
                report.raw_files_deleted = await asyncio.to_thread(
                    _purge_tenant_raw_files,
                    settings.project_root,
                    tenant_id,
                    dry_run=dry_run,
                )
            except Exception as exc:
                report.errors.append(f"raw_data: {exc}")
                logger.exception("Data purge failed for tenant raw files.")

    audit_actor = str(actor_id or "system")
    _AUDIT_LOGGER.info(
        "DATA_PURGE actor_id=%s target_user_id=%s thread_id=%s dry_run=%s status=%s report=%s",
        audit_actor,
        user_id,
        thread_id,
        dry_run,
        report.status,
        report.to_dict(),
    )
    if not dry_run:
        audit_details = {**report.to_dict(), "target_user_id": user_id}
        await asyncio.to_thread(
            record_audit_event,
            event_type="data_purge",
            actor_id=audit_actor,
            thread_id=thread_id,
            decision=report.status,
            details=audit_details,
        )
    return report


def get_rag_cache() -> Any:
    """Late import wrapper so the cache module can evolve independently."""
    from src.agentic.cache.rag_cache import get_rag_cache as _get_rag_cache

    return _get_rag_cache()


def cost_tags_from_context() -> dict[str, str]:
    """Return bounded cost-allocation tags for the active tool execution.

    The model gateway may call this helper to attach ``client_id``,
    ``department`` and ``env`` metadata to its cost metrics. The helper is
    defensive: new context fields are read via ``getattr`` so the execution
    context can be extended without a coordinated change here.
    """
    return cost_allocation_tags(str(get_settings().environment))
