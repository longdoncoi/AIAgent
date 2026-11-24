"""P0 data-lifecycle / compliance regression tests.

Covers:
* Right-to-be-Forgotten purge across PostgreSQL, Redis, LongTermStore and RAG cache.
* Idempotency and dry-run behaviour.
* Admin endpoint authentication / explicit confirmation.
* Checkpoint trimming that actually shrinks the LangGraph checkpoint.
* Redis checkpointer fallback when PostgreSQL initialization fails.
* Tenant-scoped Redis cleanup and mandatory HMAC verification.
"""

from __future__ import annotations

import fnmatch
import pickle
import re
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.messages.modifier import RemoveMessage
from langgraph.graph.message import add_messages

from src.agentic.api.main import app
from src.agentic.core.config import get_settings
from src.agentic.memory.data_lifecycle import (
    PurgeBackendUnavailable,
    PurgePermissionError,
    PurgeReport,
    cleanup_stale_postgres_checkpoints,
    cost_tags_from_context,
    purge_user_data,
)
from src.agentic.memory.long_term import LongTermStore, MemoryEntry
from src.agentic.memory.redis_saver import RedisSaver
from src.agentic.memory.short_term import initialize_checkpointer, trim_messages_window
from src.agentic.tools.execution_context import (
    ToolExecutionContext,
    reset_tool_execution_context,
    set_tool_execution_context,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeCursor:
    """Minimal DB-API cursor that records executed SQL and parameters."""

    def __init__(self, conn: FakePgConn) -> None:
        self._conn = conn
        self._rows: list[Any] = []
        self.rowcount = 0

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> FakeCursor:
        self._conn.executed.append((sql, params))
        self.rowcount = 0
        upper = sql.strip().upper()
        if upper.startswith("SELECT DISTINCT THREAD_ID"):
            self._rows = [(t,) for t in sorted(self._conn.threads)]
        elif upper.startswith("SELECT COUNT") and "AS STALE_THREADS" in upper:
            self._rows = [(self._conn.stale_count,)]
        elif upper.startswith("SELECT COUNT"):
            table = re.search(r"FROM\s+(\w+)", sql, re.I)
            name = table.group(1) if table else ""
            self._rows = [(self._conn.rows.get(name, {}).get(str(params[0]), 0),)]
        elif upper.startswith("DELETE FROM"):
            table = re.search(r"FROM\s+(\w+)", sql, re.I)
            name = table.group(1) if table else ""
            self.rowcount = self._conn.rows.get(name, {}).pop(str(params[0]), 0)
        else:  # pragma: no cover - unexpected SQL
            raise AssertionError(f"Unexpected SQL: {sql}")
        return self

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Any]:
        return list(self._rows)


class FakePgConn:
    """In-memory stand-in for a PostgreSQL connection."""

    def __init__(self, threads: list[str]) -> None:
        self.threads = set(threads)
        self.rows: dict[str, dict[str, int]] = {
            "checkpoints": {t: 1 for t in threads},
            "checkpoint_blobs": {t: 1 for t in threads},
            "checkpoint_writes": {t: 1 for t in threads},
        }
        self.executed: list[tuple[str, Any]] = []
        self.commits = 0
        self.stale_count = len(threads)

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1


class FakePoolConnection:
    def __init__(self, conn: FakePgConn) -> None:
        self.conn = conn

    def __enter__(self) -> FakePgConn:
        return self.conn

    def __exit__(self, *args: object) -> None:
        self.conn.commit()


class FakePool:
    def __init__(self, conn: FakePgConn) -> None:
        self.conn = conn

    def connection(self) -> FakePoolConnection:
        return FakePoolConnection(self.conn)


class FakeRedis:
    """SCAN-based Redis fake; deletes keys from a shared set."""

    def __init__(self, keys: list[bytes | str] | None = None) -> None:
        self.keys_set: set[bytes | str] = set(keys or [])
        self.deleted: list[str] = []

    def scan_iter(self, match: str, count: int = 200) -> Any:
        for key in list(self.keys_set):
            text = key.decode("utf-8") if isinstance(key, bytes) else key
            if fnmatch.fnmatch(text, match):
                yield key

    def delete(self, key: bytes | str) -> int:
        text = key.decode("utf-8") if isinstance(key, bytes) else str(key)
        self.deleted.append(text)
        self.keys_set.discard(key)
        return 1


class FakeLongTermStore:
    """Fake LongTermStore with stateful deletion."""

    def __init__(self, entries: list[MemoryEntry] | None = None) -> None:
        self.entries = list(entries or [])
        self.list_calls: list[str] = []
        self.delete_calls: list[str] = []

    def list_by_user(self, user_id: str) -> list[MemoryEntry]:
        self.list_calls.append(user_id)
        return [e for e in self.entries if e.metadata.get("user_id") == user_id]

    def delete_by_user(self, user_id: str) -> int:
        self.delete_calls.append(user_id)
        before = len(self.entries)
        self.entries = [e for e in self.entries if e.metadata.get("user_id") != user_id]
        return before - len(self.entries)


class FakeRagCache:
    def __init__(self, count: int = 2) -> None:
        self.count = count
        self.invalidated: list[str] = []

    def invalidate_by_source(self, source: str) -> int:
        self.invalidated.append(source)
        return self.count


class FakeKnowledgeGraph:
    def __init__(self, count: int = 2) -> None:
        self.count = count
        self.deleted: list[str] = []

    def count_tenant(self, tenant_id: str) -> int:
        return self.count

    def delete_tenant(self, tenant_id: str) -> int:
        self.deleted.append(tenant_id)
        return self.count


class FakeQdrant:
    def __init__(self, count: int = 3) -> None:
        self.result = SimpleNamespace(count=count)
        self.deleted = False

    def count(self, **kwargs: Any) -> Any:
        return self.result

    def delete(self, **kwargs: Any) -> None:
        self.deleted = True


class FakeRegistry:
    def __init__(self, owner: str | None = None, owned: list[str] | None = None) -> None:
        self._owner = owner
        self._owned = owned or []

    async def owner(self, thread_id: str) -> str | None:
        return self._owner

    async def threads_for_owner(self, owner_id: str) -> list[str]:
        return list(self._owned)

    def scoped_thread_id(self, thread_id: str, owner_id: str) -> str:
        return f"thread:scoped:{owner_id}:{thread_id}"


class FailingRegistry(FakeRegistry):
    async def owner(self, thread_id: str) -> str | None:
        raise ConnectionError("registry offline")

    async def threads_for_owner(self, owner_id: str) -> list[str]:
        raise ConnectionError("registry offline")


SCOPED = "thread:scoped:alice:public-1"


def _make_entries() -> list[MemoryEntry]:
    return [
        MemoryEntry(id="mem-1", category="semantic", content="x", metadata={"user_id": "alice"}),
        MemoryEntry(id="mem-2", category="episodic", content="y", metadata={"user_id": "alice"}),
    ]


def _make_redis_keys() -> list[bytes | str]:
    return [
        f"agentic:checkpoint:tenant:default:{SCOPED}::cp1".encode(),
        f"agentic:checkpoint:writes:tenant:default:{SCOPED}::cp1".encode(),
        f"agentic:checkpoint:index:tenant:default:{SCOPED}".encode(),
        "agentic:checkpoint:tenant:default:thread:scoped:bob:public-9::cp9".encode(),
    ]


def _patch_registry(owner: str | None = None, owned: list[str] | None = None) -> Any:
    return patch(
        "src.agentic.memory.data_lifecycle.get_thread_registry",
        return_value=FakeRegistry(owner, owned),
    )


def _fakes() -> tuple[FakePgConn, FakeRedis, FakeLongTermStore, FakeRagCache]:
    pg = FakePgConn([SCOPED])
    # The public id can also appear in legacy checkpoint rows even though
    # ownership enumeration returns only the scoped id.
    for table in pg.rows:
        pg.rows[table]["public-1"] = 1
    redis = FakeRedis(_make_redis_keys())
    ltm = FakeLongTermStore(_make_entries())
    rag = FakeRagCache()
    return pg, redis, ltm, rag


def test_postgres_checkpoint_retention_is_bounded_and_parameterized() -> None:
    pg = FakePgConn(["stale-1", "stale-2"])
    count = cleanup_stale_postgres_checkpoints(86400 * 30, pool=FakePool(pg))

    assert count == 2
    assert pg.commits == 1
    assert len([sql for sql, _ in pg.executed if sql.startswith("DELETE FROM")]) == 3
    assert all(params == (86400 * 30,) for _, params in pg.executed)
    assert all("2592000" not in sql for sql, _ in pg.executed)

    with pytest.raises(ValueError, match="max_idle_seconds"):
        cleanup_stale_postgres_checkpoints(1, pool=FakePool(pg))


def test_postgres_checkpoint_retention_fails_closed_without_backend() -> None:
    with patch(
        "src.agentic.memory.data_lifecycle.get_postgres_pool", return_value=None
    ), pytest.raises(PurgeBackendUnavailable):
        cleanup_stale_postgres_checkpoints(86400 * 30)


# ---------------------------------------------------------------------------
# Purge service
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_purge_deletes_from_all_four_stores() -> None:
    """Purge must touch Postgres, Redis, LongTermStore and RAG cache."""
    pg, redis, ltm, rag = _fakes()
    with _patch_registry("alice"):
        report = await purge_user_data(
            "alice",
            "public-1",
            pg_conn=pg,
            redis_client=redis,
            long_term_store=ltm,
            rag_cache=rag,
        )

    assert isinstance(report, PurgeReport)
    assert report.status == "ok"
    assert set(report.checkpoint_thread_ids) == {SCOPED, "public-1"}
    # Every checkpoint table was deleted from, with bound parameters only.
    assert report.postgres_rows_deleted == {
        "checkpoints": 2,
        "checkpoint_blobs": 2,
        "checkpoint_writes": 2,
    }
    assert pg.commits == 1
    assert all("alice" not in sql and "public-1" not in sql for sql, _ in pg.executed)
    assert all(params is not None for _, params in pg.executed)

    # Redis: exactly the three alice keys, never bob's.
    assert report.redis_keys_deleted == 3
    assert all("bob" not in key for key in redis.deleted)
    # bob's checkpoint key is untouched (still present in the fake).
    assert any(b"bob" in key for key in redis.keys_set)

    # Long-term memory + RAG cache.
    assert report.long_term_entries_deleted == 2
    assert ltm.delete_calls == ["alice"]
    assert report.rag_cache_entries_invalidated == 2
    assert rag.invalidated == ["alice"]
    assert report.to_dict()["status"] == "ok"


@pytest.mark.asyncio
async def test_purge_is_idempotent() -> None:
    """Running the same purge twice must report zero deletions the second time."""
    pg, redis, ltm, rag = _fakes()
    with _patch_registry("alice"):
        first = await purge_user_data(
            "alice",
            "public-1",
            pg_conn=pg,
            redis_client=redis,
            long_term_store=ltm,
            rag_cache=rag,
        )
        second = await purge_user_data(
            "alice",
            "public-1",
            pg_conn=pg,
            redis_client=redis,
            long_term_store=ltm,
            rag_cache=rag,
        )

    assert sum(first.postgres_rows_deleted.values()) == 6
    assert sum(second.postgres_rows_deleted.values()) == 0
    assert first.redis_keys_deleted == 3
    assert second.redis_keys_deleted == 0
    assert first.long_term_entries_deleted == 2
    assert second.long_term_entries_deleted == 0
    assert second.status == "ok"
    assert second.errors == []


@pytest.mark.asyncio
async def test_purge_dry_run_does_not_mutate() -> None:
    """Dry-run reports what would be deleted but performs no writes."""
    pg, redis, ltm, rag = _fakes()
    with _patch_registry("alice"):
        report = await purge_user_data(
            "alice",
            "public-1",
            dry_run=True,
            pg_conn=pg,
            redis_client=redis,
            long_term_store=ltm,
            rag_cache=rag,
        )

    assert report.status == "dry_run"
    assert report.dry_run is True
    assert sum(report.postgres_rows_deleted.values()) == 6
    assert report.redis_keys_deleted == 3
    assert report.long_term_entries_deleted == 2
    # No mutations:
    assert pg.commits == 0
    assert not any(sql.strip().upper().startswith("DELETE") for sql, _ in pg.executed)
    assert redis.deleted == []
    assert ltm.delete_calls == []
    assert rag.invalidated == []


@pytest.mark.asyncio
async def test_purge_rejects_foreign_thread() -> None:
    """A thread owned by another principal must never be purged."""
    pg, redis, ltm, rag = _fakes()
    with _patch_registry("bob"), pytest.raises(PurgePermissionError):
        await purge_user_data(
            "alice",
            "public-1",
            pg_conn=pg,
            redis_client=redis,
            long_term_store=ltm,
            rag_cache=rag,
        )
    assert pg.executed == []
    assert redis.deleted == []
    assert ltm.delete_calls == []


@pytest.mark.asyncio
async def test_purge_fails_closed_when_thread_ownership_is_unavailable() -> None:
    pg, redis, ltm, rag = _fakes()
    with patch(
        "src.agentic.memory.data_lifecycle.get_thread_registry",
        return_value=FailingRegistry(),
    ), pytest.raises(PurgeBackendUnavailable):
        await purge_user_data(
            "alice",
            "public-1",
            pg_conn=pg,
            redis_client=redis,
            long_term_store=ltm,
            rag_cache=rag,
        )
    assert pg.executed == []
    assert redis.deleted == []
    assert ltm.delete_calls == []


def test_long_term_primary_keys_are_tenant_namespaced(tmp_path: Any) -> None:
    store = LongTermStore(storage_dir=tmp_path, force_sqlite=True)
    store.add_semantic("theme", "dark", {"user_id": "alice"})
    store.add_semantic("theme", "light", {"user_id": "bob"})

    alice = store.search("dark", user_id="alice")
    bob = store.search("light", user_id="bob")
    assert len(alice) == len(bob) == 1
    assert alice[0].id != bob[0].id
    assert store.search("light", user_id="alice") == []


@pytest.mark.asyncio
async def test_user_wide_purge_enumerates_owned_threads() -> None:
    """Without a thread_id, all threads owned by the user must be discovered."""
    pg, redis, ltm, rag = _fakes()
    kg = FakeKnowledgeGraph()
    qdrant = FakeQdrant()
    with _patch_registry(None, owned=["public-1"]):
        report = await purge_user_data(
            "alice",
            pg_conn=pg,
            redis_client=redis,
            long_term_store=ltm,
            rag_cache=rag,
            qdrant_client=qdrant,
            knowledge_graph_store=kg,
        )
    assert SCOPED in report.checkpoint_thread_ids
    assert "public-1" in report.checkpoint_thread_ids
    assert report.redis_keys_deleted == 3
    assert report.qdrant_points_deleted == 3
    assert qdrant.deleted is True
    assert report.knowledge_graph_nodes_deleted == 2
    assert kg.deleted == ["alice"]


@pytest.mark.asyncio
async def test_purge_validates_identifier() -> None:
    with pytest.raises(ValueError, match="user_id"):
        await purge_user_data("bad id; DROP TABLE x", dry_run=True)


# ---------------------------------------------------------------------------
# Admin endpoint
# ---------------------------------------------------------------------------
def _admin_headers(role: str = "admin", subject: str = "admin-1") -> dict[str, str]:
    settings = get_settings()
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": subject,
            "role": role,
            "aud": settings.jwt_audience,
            "iss": settings.jwt_issuer,
            "iat": now,
            "exp": now + 3600,
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    return {"Authorization": f"Bearer {token}"}


def test_admin_purge_endpoint_requires_auth_and_confirmation() -> None:
    """Non-admins get 403, malformed auth gets 401, and unconfirmed purge gets 400."""
    report = PurgeReport(user_id="alice", dry_run=False)
    with patch(
        "src.agentic.api.routes.admin.purge_user_data", new_callable=AsyncMock
    ) as mock_purge:
        mock_purge.return_value = report
        client = TestClient(app)

        # 1. Non-admin token -> 403
        resp = client.post(
            "/api/v1/admin/users/alice/purge",
            headers=_admin_headers(role="user"),
            params={"confirm": "true"},
        )
        assert resp.status_code == 403
        mock_purge.assert_not_called()

        # 2. Malformed auth header -> 401
        resp = client.post(
            "/api/v1/admin/users/alice/purge",
            headers={"Authorization": "InvalidFormat"},
            params={"confirm": "true"},
        )
        assert resp.status_code == 401

        # 3. Admin but no confirmation -> 400
        resp = client.post(
            "/api/v1/admin/users/alice/purge",
            headers=_admin_headers(),
        )
        assert resp.status_code == 400
        assert "confirmation" in resp.json()["detail"]
        mock_purge.assert_not_called()

        # 4. Admin with explicit confirmation -> 200, service invoked
        resp = client.post(
            "/api/v1/admin/users/alice/purge",
            headers=_admin_headers(),
            params={"confirm": "true"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        mock_purge.assert_awaited_once_with("alice", None, dry_run=False)


def test_admin_purge_endpoint_dry_run_body_confirmation() -> None:
    """Confirmation via request body is honoured; dry-run bypasses the guard."""
    report = PurgeReport(user_id="alice", dry_run=True)
    with patch(
        "src.agentic.api.routes.admin.purge_user_data", new_callable=AsyncMock
    ) as mock_purge:
        mock_purge.return_value = report
        client = TestClient(app)

        resp = client.post(
            "/api/v1/admin/users/alice/purge",
            headers=_admin_headers(),
            json={"confirm": True, "thread_id": "public-1"},
        )
        assert resp.status_code == 200
        mock_purge.assert_awaited_with("alice", "public-1", dry_run=False)

        mock_purge.reset_mock()
        resp = client.post(
            "/api/v1/admin/users/alice/purge",
            headers=_admin_headers(),
            params={"dry_run": "true"},
        )
        assert resp.status_code == 200
        mock_purge.assert_awaited_with("alice", None, dry_run=True)


# ---------------------------------------------------------------------------
# Checkpoint trimming
# ---------------------------------------------------------------------------
def test_trim_messages_window_emits_remove_messages() -> None:
    """Dropped ids must become RemoveMessage entries so the checkpoint shrinks."""
    sys_msg = SystemMessage(content="system", id="m-sys")
    messages = [sys_msg] + [
        HumanMessage(content=f"msg-{i}", id=f"m-{i}") for i in range(10)
    ]

    result = trim_messages_window(messages, max_tokens=2)
    removals = [m for m in result if isinstance(m, RemoveMessage)]
    assert removals
    assert {m.id for m in removals}.issubset({m.id for m in messages if m.id})

    # Feeding the result through LangGraph's reducer actually shrinks state.
    state = add_messages(messages, removals)
    assert len(state) < len(messages)
    assert all(m.id != removals[0].id for m in state)


def test_trim_messages_window_legacy_view_without_removals() -> None:
    """The opt-out flag preserves the original view-only behaviour."""
    messages = [HumanMessage(content="x" * 100, id=f"m-{i}") for i in range(5)]
    result = trim_messages_window(messages, max_tokens=10, emit_removals=False)
    assert not any(isinstance(m, RemoveMessage) for m in result)
    assert len(result) < len(messages)


# ---------------------------------------------------------------------------
# Redis fallback
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_redis_fallback_reachable_when_postgres_down() -> None:
    """When Postgres init fails and Redis is enabled, RedisSaver is constructed."""
    from src.agentic.memory import short_term

    settings = get_settings()
    sentinel = MagicMock(name="RedisSaverSentinel")
    with (
        patch.object(settings, "use_postgres_checkpoint", True),
        patch.object(settings, "use_redis_checkpoint", True),
        patch.object(settings, "environment", "test"),
        patch.object(short_term, "_global_checkpointer", None),
        patch.object(short_term, "_pg_saver_cm", None),
        patch(
            "langgraph.checkpoint.postgres.aio.AsyncPostgresSaver.from_conn_string",
            side_effect=ConnectionError("postgres down"),
        ),
        patch("src.agentic.memory.redis_saver.RedisSaver", return_value=sentinel),
    ):
        saver = await initialize_checkpointer()

    assert saver is sentinel


@pytest.mark.asyncio
async def test_redis_fallback_fails_closed_in_production() -> None:
    """Production must not silently fall back to MemorySaver."""
    from src.agentic.memory import short_term

    settings = get_settings()
    with (
        patch.object(settings, "use_postgres_checkpoint", True),
        patch.object(settings, "use_redis_checkpoint", True),
        patch.object(settings, "environment", "production"),
        patch.object(short_term, "_global_checkpointer", None),
        patch(
            "langgraph.checkpoint.postgres.aio.AsyncPostgresSaver.from_conn_string",
            side_effect=ConnectionError("postgres down"),
        ),
        patch(
            "src.agentic.memory.redis_saver.RedisSaver",
            side_effect=ConnectionError("redis down"),
        ),
        pytest.raises(RuntimeError, match="both PostgreSQL and Redis"),
    ):
        await initialize_checkpointer()


# ---------------------------------------------------------------------------
# Redis saver hardening
# ---------------------------------------------------------------------------
def test_redis_cleanup_rejects_cross_tenant_scope() -> None:
    saver = RedisSaver(redis_url="", tenant_scope="tenant-a")
    with pytest.raises(PermissionError, match="Cross-tenant"):
        saver.cleanup_inactive_threads(tenant_scope="tenant-b")


def test_redis_rejects_unsigned_payload_even_when_legacy_flag_set() -> None:
    saver = RedisSaver.__new__(RedisSaver)
    saver.settings = SimpleNamespace(
        allow_legacy_unsigned_checkpoints=True, environment="production"
    )
    saver._secret_key = b"key"
    assert saver._verify_and_deserialize(pickle.dumps({"unsafe": True})) is None


# ---------------------------------------------------------------------------
# Cost allocation tags
# ---------------------------------------------------------------------------
def test_cost_tags_from_context() -> None:
    context = ToolExecutionContext(
        agent_name="chat",
        step_id="s1",
        owner_id="acme-corp",
        client_id="acme-corp",
    )
    token = set_tool_execution_context(context)
    try:
        tags = cost_tags_from_context()
        assert tags["client_id"] == "acme-corp"
        assert tags["env"] == get_settings().environment
        assert "agent" in tags
    finally:
        reset_tool_execution_context(token)
