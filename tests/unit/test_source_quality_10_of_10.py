"""High-value source-quality tests for security, audit, tracing, and async I/O."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from evals.dataset_io import load_jsonl
from src.agentic.a2a.executor import A2ATaskPayload, A2ATaskRequest
from src.agentic.a2a.identity import sign_request, verify_request
from src.agentic.core.config import Settings
from src.agentic.memory import migrations, short_term
from src.agentic.memory.thread_registry import ThreadRegistry
from src.agentic.observability import tracing
from src.agentic.safety import audit
from src.agentic.tools.servers.security import (
    MCPServiceAuthMiddleware,
    run_mcp_server,
)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _keypair() -> tuple[str, str]:
    private = Ed25519PrivateKey.generate()
    return _b64(private.private_bytes_raw()), _b64(private.public_key().public_bytes_raw())


def _request() -> A2ATaskRequest:
    return A2ATaskRequest(
        task_id="step-1",
        target_agent="coding",
        payload=A2ATaskPayload(instruction="inspect repository", context={"path": "src"}),
        metadata={"tenant_id": "tenant-a", "owner_id": "alice", "user_roles": ["developer"]},
        idempotency_key="turn-1:step-1",
    )


@pytest.mark.asyncio
async def test_jsonl_loader_is_nonblocking_and_validates_records(tmp_path: Path) -> None:
    dataset = tmp_path / "valid.jsonl"
    dataset.write_text('{"id": 1}\n\n  {"id": 2}\n', encoding="utf-8")
    assert await load_jsonl(dataset) == [{"id": 1}, {"id": 2}]

    dataset.write_text('["not", "an", "object"]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="JSONL record must be an object"):
        await load_jsonl(dataset)

    dataset.write_text("{broken json}\n", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        await load_jsonl(dataset)


def test_a2a_identity_sign_verify_tamper_and_automatic_key_id() -> None:
    private, public = _keypair()
    signer = Settings(environment="test", approval_private_key=private)
    verifier = Settings(environment="test", approval_public_key=public)
    request = _request()

    with patch("src.agentic.a2a.identity.time.time", return_value=1_000):
        sign_request(request, signer)
        verify_request(request, verifier)

    identity = request.metadata["_identity"]
    assert identity["kid"] == hashlib.sha256(base64.urlsafe_b64decode(public + "=")).hexdigest()[:16]

    request.payload.context["path"] = "secrets"
    with (
        patch("src.agentic.a2a.identity.time.time", return_value=1_000),
        pytest.raises(InvalidSignature),
    ):
        verify_request(request, verifier)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_agent", "rag"),
        ("caller", "untrusted-service"),
        ("task_id", "step-2"),
        ("idempotency_key", "replayed"),
    ],
)
def test_a2a_identity_covers_all_routing_fields(field: str, value: str) -> None:
    private, public = _keypair()
    cfg = Settings(
        environment="test",
        approval_private_key=private,
        approval_public_key=public,
        approval_key_id="active-kid",
    )
    request = _request()
    with patch("src.agentic.a2a.identity.time.time", return_value=5_000):
        sign_request(request, cfg)
        setattr(request, field, value)
        with pytest.raises(InvalidSignature):
            verify_request(request, cfg)


def test_a2a_identity_rotation_and_fail_closed_branches() -> None:
    old_private, old_public = _keypair()
    _new_private, new_public = _keypair()
    old_cfg = Settings(
        environment="test",
        approval_private_key=old_private,
        approval_key_id="old-kid",
    )
    rotated_cfg = Settings(
        environment="test",
        approval_public_key=new_public,
        approval_key_id="new-kid",
        approval_previous_public_keys={"old-kid": old_public},
    )
    request = _request()
    with patch("src.agentic.a2a.identity.time.time", return_value=10_000):
        sign_request(request, old_cfg)
        verify_request(request, rotated_cfg)

        request.metadata["_identity"]["kid"] = "unknown-kid"
        with pytest.raises(ValueError, match="unavailable or untrusted"):
            verify_request(request, rotated_cfg)

    request.metadata["_identity"]["kid"] = "old-kid"
    with (
        patch("src.agentic.a2a.identity.time.time", return_value=10_121),
        pytest.raises(ValueError, match="stale"),
    ):
        verify_request(request, rotated_cfg)

    request.metadata["_identity"].pop("kid")
    with (
        patch("src.agentic.a2a.identity.time.time", return_value=10_000),
        pytest.raises(ValueError, match="key id"),
    ):
        verify_request(request, rotated_cfg)


def test_a2a_identity_can_verify_from_previous_keyring_only() -> None:
    private, public = _keypair()
    request = _request()
    with patch("src.agentic.a2a.identity.time.time", return_value=20_000):
        sign_request(
            request,
            Settings(
                environment="test", approval_private_key=private, approval_key_id="retired-kid"
            ),
        )
        verify_request(
            request,
            Settings(
                environment="test",
                approval_public_key=None,
                approval_previous_public_keys={"retired-kid": public},
            ),
        )


def test_a2a_identity_missing_key_policy() -> None:
    request = _request()
    permissive = Settings(environment="test", approval_private_key=None, approval_public_key=None)
    sign_request(request, permissive)
    verify_request(request, permissive)
    assert "_identity" not in request.metadata

    with (
        patch("src.agentic.a2a.identity.is_strict_env", return_value=True),
        pytest.raises(ValueError, match="signing key"),
    ):
        sign_request(request, permissive)
    with (
        patch("src.agentic.a2a.identity.is_strict_env", return_value=True),
        pytest.raises(ValueError, match="missing signed"),
    ):
        verify_request(request, permissive)


def _postgres_audit_row(
    *,
    event_id: str,
    key: str,
    previous_hash: str,
    created: datetime | str,
) -> tuple[object, ...]:
    created_iso = (
        created.replace(tzinfo=UTC).isoformat()
        if isinstance(created, datetime) and created.tzinfo is None
        else created.astimezone(UTC).isoformat()
        if isinstance(created, datetime)
        else str(created)
    )
    details = {
        "action": "approve",
        "_integrity_version": 2,
        "_integrity_trace_id": "trace-1",
        "_integrity_risk_level": "high",
    }
    event_hash = audit.compute_audit_integrity_hash(
        event_id=event_id,
        event_type="approval",
        actor_id="reviewer",
        thread_id="thread-1",
        step_id="step-1",
        trace_id="trace-1",
        decision="approved",
        risk_level="high",
        details=details,
        created_at=created_iso,
        signing_key=key,
    )
    chained_hash = hmac.new(
        key.encode(), f"{previous_hash}:{event_hash}".encode(), hashlib.sha256
    ).hexdigest()
    return (
        event_id,
        "approval",
        "reviewer",
        "thread-1",
        "step-1",
        "trace-1",
        "approved",
        "high",
        {**details, "prev_hash": previous_hash},
        chained_hash,
        created,
    )


def _mock_postgres_rows(monkeypatch: pytest.MonkeyPatch, rows: list[tuple[object, ...]]) -> None:
    connect_cm = MagicMock()
    connection = MagicMock()
    cursor_cm = MagicMock()
    cursor = MagicMock()
    connect_cm.__enter__.return_value = connection
    connection.cursor.return_value = cursor_cm
    cursor_cm.__enter__.return_value = cursor
    cursor.fetchall.return_value = rows
    monkeypatch.setattr(psycopg, "connect", lambda *_args, **_kwargs: connect_cm)


def test_postgres_audit_verification_supports_timezones_and_rotated_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_key = "current-audit-key"
    previous_key = "previous-audit-key"
    first = _postgres_audit_row(
        event_id="event-1",
        key=current_key,
        previous_hash="GENESIS",
        created=datetime(2026, 1, 2, 3, 4, 5),
    )
    second = _postgres_audit_row(
        event_id="event-2",
        key=previous_key,
        previous_hash=str(first[9]),
        created="2026-01-02T03:05:05+00:00",
    )
    _mock_postgres_rows(monkeypatch, [first, second])
    cfg = Settings(
        environment="test",
        checkpoint_hmac_secret=current_key,
        checkpoint_hmac_previous_secrets=[previous_key],
    )

    assert audit.verify_audit_chain(settings=cfg) == (True, 2, [])


def test_postgres_audit_verification_reports_chain_and_hmac_tampering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = list(
        _postgres_audit_row(
            event_id="event-bad",
            key="audit-key",
            previous_hash="WRONG-PREVIOUS",
            created=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        )
    )
    row[9] = "forged-hash"
    _mock_postgres_rows(monkeypatch, [tuple(row)])
    valid, count, errors = audit.verify_audit_chain(
        settings=Settings(environment="test", checkpoint_hmac_secret="audit-key")
    )
    assert valid is False
    assert count == 1
    assert any("prev_hash mismatch" in error for error in errors)
    assert any("chained HMAC verification failed" in error for error in errors)


def test_sqlite_audit_empty_and_broken_continuity(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    assert audit.verify_audit_chain(sqlite_path=missing) == (True, 0, [])

    cfg = Settings(environment="test", checkpoint_hmac_secret="sqlite-audit-key")
    audit.record_audit_event(
        event_type="approval",
        actor_id="reviewer",
        details={"sequence": 1},
        settings=cfg,
        sqlite_path=missing,
    )
    with audit.sqlite3.connect(missing) as connection:
        details = json.loads(connection.execute("SELECT details FROM audit_logs").fetchone()[0])
        details["prev_hash"] = "forged"
        connection.execute("UPDATE audit_logs SET details = ?", (json.dumps(details),))
        connection.commit()
    valid, count, errors = audit.verify_audit_chain(settings=cfg, sqlite_path=missing)
    assert valid is False and count == 1
    assert any("prev_hash mismatch" in error for error in errors)
    assert any("HMAC integrity verification failed" in error for error in errors)


def test_audit_legacy_hash_and_persistence_failure_fallbacks(tmp_path: Path) -> None:
    legacy = audit.compute_audit_integrity_hash(
        event_id="legacy",
        event_type="event",
        actor_id="actor",
        thread_id=None,
        step_id=None,
        decision=None,
        details={"legacy": True},
        created_at="2026-01-01T00:00:00+00:00",
        signing_key="legacy-key",
    )
    assert len(legacy) == 64

    cfg = Settings(
        environment="test",
        checkpoint_hmac_secret="fallback-key",
        use_postgres_checkpoint=True,
    )
    sqlite_path = tmp_path / "fallback.db"
    with patch.object(audit, "_write_postgres_audit", side_effect=OSError("postgres down")):
        event_id = audit.record_audit_event(
            event_type="fallback",
            actor_id="actor",
            settings=cfg,
            sqlite_path=sqlite_path,
        )
    assert event_id
    assert audit.verify_audit_chain(settings=cfg, sqlite_path=sqlite_path)[0] is True

    with patch.object(audit, "_write_sqlite_audit", side_effect=OSError("disk full")):
        assert audit.record_audit_event(
            event_type="best-effort-local",
            actor_id="actor",
            settings=Settings(environment="test", checkpoint_hmac_secret="local-key"),
            sqlite_path=tmp_path / "broken.db",
        )


def test_configure_tracing_idempotent_console_and_fail_closed() -> None:
    cfg = Settings(environment="test", enable_tracing=False)
    existing = tracing.TracerProvider()
    with (
        patch.object(tracing, "setup_langsmith") as setup,
        patch.object(tracing.trace, "get_tracer_provider", return_value=existing),
    ):
        assert tracing.configure_tracing(cfg) is None
        setup.assert_called_once_with(cfg)

    console_cfg = Settings(
        environment="test",
        enable_tracing=True,
        debug=True,
        otel_exporter_otlp_endpoint="",
    )
    with (
        patch.object(tracing.trace, "get_tracer_provider", return_value=object()),
        patch.object(tracing.trace, "set_tracer_provider") as set_provider,
        patch.object(tracing, "BatchSpanProcessor", return_value=MagicMock()),
        patch.object(tracing, "ConsoleSpanExporter", return_value=MagicMock()),
    ):
        provider = tracing.configure_tracing(console_cfg)
        assert provider is not None
        set_provider.assert_called_once_with(provider)

    with (
        patch.object(tracing.trace, "get_tracer_provider", return_value=object()),
        patch.object(tracing, "is_strict_env", return_value=True),
        pytest.raises(RuntimeError, match="requires an OTLP exporter endpoint"),
    ):
        tracing.configure_tracing(console_cfg.model_copy(update={"debug": False}))


def test_configure_tracing_otlp_success_and_failure() -> None:
    cfg = Settings(
        environment="test",
        enable_tracing=True,
        otel_exporter_otlp_endpoint="http://collector:4317",
    )
    exporter_path = "opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter"
    with (
        patch.object(tracing.trace, "get_tracer_provider", return_value=object()),
        patch.object(tracing.trace, "set_tracer_provider") as set_provider,
        patch.object(tracing, "BatchSpanProcessor", return_value=MagicMock()),
        patch(exporter_path, return_value=MagicMock()) as exporter,
    ):
        provider = tracing.configure_tracing(cfg)
        assert provider is not None
        exporter.assert_called_once_with(endpoint="http://collector:4317", insecure=True)
        set_provider.assert_called_once_with(provider)

    with (
        patch.object(tracing.trace, "get_tracer_provider", return_value=object()),
        patch.object(tracing, "is_strict_env", return_value=True),
        patch(exporter_path, side_effect=OSError("collector unavailable")),
        pytest.raises(RuntimeError, match="working OTLP exporter"),
    ):
        tracing.configure_tracing(cfg)

    with (
        patch.object(tracing.trace, "get_tracer_provider", return_value=object()),
        patch.object(tracing.trace, "set_tracer_provider") as set_provider,
        patch.object(tracing, "is_strict_env", return_value=False),
        patch(exporter_path, side_effect=OSError("collector unavailable")),
    ):
        provider = tracing.configure_tracing(cfg)
        set_provider.assert_called_once_with(provider)

    disabled_export_cfg = cfg.model_copy(
        update={"otel_exporter_otlp_endpoint": "", "debug": False}
    )
    with (
        patch.object(tracing.trace, "get_tracer_provider", return_value=object()),
        patch.object(tracing.trace, "set_tracer_provider") as set_provider,
        patch.object(tracing, "is_strict_env", return_value=False),
    ):
        provider = tracing.configure_tracing(disabled_export_cfg)
        set_provider.assert_called_once_with(provider)


@pytest.mark.asyncio
async def test_trace_span_without_attributes() -> None:
    async with tracing.trace_span("quality.no-attributes") as span:
        assert span is not None

    async with tracing.trace_span("quality.none-attribute", {"ignored": None}) as span:
        assert span is not None


def test_migration_advisory_lock_lifecycle_and_failures() -> None:
    connection = MagicMock()
    with patch("psycopg.connect", return_value=connection) as connect:
        assert migrations._acquire_advisory_lock("postgresql+psycopg://u:p@db/app") is connection
    connect.assert_called_once_with("postgresql://u:p@db/app", connect_timeout=3)
    connection.execute.assert_called_once()

    with patch("psycopg.connect", side_effect=OSError("offline")):
        assert migrations._acquire_advisory_lock("postgresql://u:p@db/app") is None

    migrations._release_advisory_lock(None)
    connection.reset_mock()
    migrations._release_advisory_lock(connection)
    connection.execute.assert_called_once()
    connection.close.assert_called_once()

    broken = MagicMock()
    broken.execute.side_effect = OSError("unlock failed")
    broken.close.side_effect = OSError("close failed")
    migrations._release_advisory_lock(broken)


def test_migration_config_unreachable_check_and_online_inspector(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        project_root=tmp_path,
        postgres_migration_uri=None,
        postgres_uri="sqlite:///quality.db",
    )
    with patch.object(migrations, "get_settings", return_value=settings):
        cfg = migrations.get_alembic_config()
    assert cfg.get_main_option("sqlalchemy.url") == "sqlite:///quality.db"

    with (
        patch.object(migrations, "get_settings", return_value=settings),
        patch.object(migrations, "_is_db_reachable", return_value=False),
        pytest.raises(RuntimeError, match="cannot verify schema"),
    ):
        migrations.check_database_migrations()

    inspector = object()
    sa_module = SimpleNamespace(inspect=lambda _bind: inspector)
    with patch("alembic.context.is_offline_mode", return_value=False):
        assert migrations.safe_alembic_inspect(sa_module, object()) is inspector


def test_run_migrations_serializes_and_releases_lock() -> None:
    lock = MagicMock()
    cfg = MagicMock()
    upgrade_calls: list[tuple[object, str]] = []

    def upgrade(target: object, revision: str) -> None:
        upgrade_calls.append((target, revision))

    with (
        patch.object(migrations, "get_settings") as get_settings,
        patch.object(migrations, "_is_db_reachable", return_value=True),
        patch.object(migrations, "_acquire_advisory_lock", return_value=lock),
        patch.object(migrations, "_release_advisory_lock") as release,
        patch.object(migrations, "get_alembic_config", return_value=cfg),
        patch.object(migrations.command, "upgrade", upgrade),
    ):
        get_settings.return_value = SimpleNamespace(
            postgres_migration_uri=None, postgres_uri="postgresql://u:p@db/app"
        )
        assert migrations.run_database_migrations() is True
        assert upgrade_calls == [(cfg, "head")]
        release.assert_called_once_with(lock)

    with (
        patch.object(migrations, "get_settings") as get_settings,
        patch.object(migrations, "_is_db_reachable", return_value=True),
        patch.object(migrations, "_acquire_advisory_lock", return_value=lock),
        patch.object(migrations, "_release_advisory_lock") as release,
        patch.object(migrations, "get_alembic_config", side_effect=ValueError("bad config")),
        pytest.raises(RuntimeError, match="Database migration to head failed"),
    ):
        get_settings.return_value = SimpleNamespace(
            postgres_migration_uri=None, postgres_uri="postgresql://u:p@db/app"
        )
        migrations.run_database_migrations()
    release.assert_called_once_with(lock)

    with (
        patch.object(migrations, "get_settings") as get_settings,
        patch.object(migrations, "_is_db_reachable", return_value=False),
        pytest.raises(RuntimeError, match="not reachable"),
    ):
        get_settings.return_value = SimpleNamespace(
            postgres_migration_uri=None, postgres_uri="postgresql://u:p@db/app"
        )
        migrations.run_database_migrations()


def test_check_migrations_disposes_engine_and_reports_mismatch() -> None:
    engine = MagicMock()
    connection = MagicMock()
    engine.connect.return_value.__enter__.return_value = connection
    script = MagicMock()
    script.get_heads.return_value = ["head-2"]
    context = MagicMock()
    context.get_current_heads.return_value = ("head-1",)
    cfg = MagicMock()
    cfg.get_main_option.return_value = "sqlite:///test.db"
    with (
        patch.object(migrations, "get_settings") as get_settings,
        patch.object(migrations, "_is_db_reachable", return_value=True),
        patch.object(migrations, "get_alembic_config", return_value=cfg),
        patch.object(migrations.ScriptDirectory, "from_config", return_value=script),
        patch.object(migrations, "create_engine", return_value=engine),
        patch.object(migrations.MigrationContext, "configure", return_value=context),
    ):
        get_settings.return_value = SimpleNamespace(postgres_uri="sqlite:///test.db")
        assert migrations.check_database_migrations() is False
    engine.dispose.assert_called_once()

    context.get_current_heads.return_value = ("head-2",)
    engine.reset_mock()
    with (
        patch.object(migrations, "get_settings") as get_settings,
        patch.object(migrations, "_is_db_reachable", return_value=True),
        patch.object(migrations, "get_alembic_config", return_value=cfg),
        patch.object(migrations.ScriptDirectory, "from_config", return_value=script),
        patch.object(migrations, "create_engine", return_value=engine),
        patch.object(migrations.MigrationContext, "configure", return_value=context),
    ):
        get_settings.return_value = SimpleNamespace(postgres_uri="sqlite:///test.db")
        assert migrations.check_database_migrations() is True


def test_postgres_pool_initialization_cache_failure_and_close() -> None:
    cfg = Settings(environment="test", use_postgres_checkpoint=True)
    pool = MagicMock()
    with (
        patch.object(short_term, "_pg_pool", None),
        patch.object(short_term, "get_settings", return_value=cfg),
        patch("psycopg_pool.ConnectionPool", return_value=pool) as pool_cls,
    ):
        assert short_term.get_postgres_pool() is pool
        assert short_term.get_postgres_pool() is pool
        pool_cls.assert_called_once()
        pool.open.assert_called_once()

    with (
        patch.object(short_term, "_pg_pool", None),
        patch.object(short_term, "get_settings", return_value=cfg),
        patch("psycopg_pool.ConnectionPool", side_effect=OSError("pool unavailable")),
    ):
        assert short_term.get_postgres_pool() is None

    context = MagicMock()
    pool = MagicMock()
    with (
        patch.object(short_term, "_pg_saver_cm", context),
        patch.object(short_term, "_pg_pool", pool),
        patch.object(short_term, "_global_checkpointer", object()),
    ):
        short_term.close_postgres_pool()
        context.__exit__.assert_called_once_with(None, None, None)
        pool.close.assert_called_once()
        assert short_term._pg_pool is None
        assert short_term._global_checkpointer is None

    context = MagicMock()
    context.__exit__.side_effect = OSError("context close failed")
    pool = MagicMock()
    pool.close.side_effect = OSError("pool close failed")
    with (
        patch.object(short_term, "_pg_saver_cm", context),
        patch.object(short_term, "_pg_pool", pool),
    ):
        short_term.close_postgres_pool()


class _AsyncConnectionContext:
    def __init__(self, connection: object) -> None:
        self.connection = connection

    async def __aenter__(self) -> object:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_checkpointer_health_checks_active_backend() -> None:
    execute = AsyncMock()
    pool = MagicMock()
    pool.connection.return_value = _AsyncConnectionContext(SimpleNamespace(execute=execute))
    with (
        patch.object(short_term, "_global_checkpointer", object()),
        patch.object(short_term, "_pg_async_pool", pool),
    ):
        assert await short_term.is_checkpointer_healthy() is True
        execute.assert_awaited_once_with("SELECT 1;")

    execute = AsyncMock(side_effect=OSError("database down"))
    pool.connection.return_value = _AsyncConnectionContext(SimpleNamespace(execute=execute))
    with (
        patch.object(short_term, "_global_checkpointer", object()),
        patch.object(short_term, "_pg_async_pool", pool),
    ):
        assert await short_term.is_checkpointer_healthy() is False

    saver_connection = SimpleNamespace(execute=AsyncMock())
    with (
        patch.object(short_term, "_global_checkpointer", SimpleNamespace(conn=saver_connection)),
        patch.object(short_term, "_pg_async_pool", None),
    ):
        assert await short_term.is_checkpointer_healthy() is True

    saver_connection.execute.side_effect = OSError("redis down")
    with (
        patch.object(short_term, "_global_checkpointer", SimpleNamespace(conn=saver_connection)),
        patch.object(short_term, "_pg_async_pool", None),
    ):
        assert await short_term.is_checkpointer_healthy() is False

    with patch.object(short_term, "_global_checkpointer", None):
        assert await short_term.is_checkpointer_healthy() is False


@pytest.mark.asyncio
async def test_close_checkpointer_cleans_all_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    context = MagicMock()
    context.__aexit__ = AsyncMock()
    pool = SimpleNamespace(close=AsyncMock())

    class FakeRedisSaver:
        def __init__(self) -> None:
            self.aclose = AsyncMock()

    import src.agentic.memory.redis_saver as redis_saver_module

    monkeypatch.setattr(redis_saver_module, "RedisSaver", FakeRedisSaver)
    saver = FakeRedisSaver()
    with (
        patch.object(short_term, "_pg_saver_cm", context),
        patch.object(short_term, "_pg_async_pool", pool),
        patch.object(short_term, "_global_checkpointer", saver),
        patch.object(short_term, "close_postgres_pool") as close_sync,
    ):
        await short_term.close_checkpointer()
        context.__aexit__.assert_awaited_once_with(None, None, None)
        pool.close.assert_awaited_once()
        saver.aclose.assert_awaited_once()
        close_sync.assert_called_once()
        assert short_term._pg_saver_cm is None
        assert short_term._pg_async_pool is None
        assert short_term._global_checkpointer is None


@pytest.mark.asyncio
async def test_initialize_checkpointer_pooled_and_strict_failure() -> None:
    cfg = Settings(environment="test", use_postgres_checkpoint=True)
    pool = MagicMock()
    pool.open = AsyncMock()
    created_savers: list[object] = []

    class FakeAsyncPostgresSaver:
        def __init__(self, *, conn: object) -> None:
            self.conn = conn
            self.setup = AsyncMock()
            created_savers.append(self)

        @staticmethod
        def from_conn_string(_uri: str) -> object:
            raise AssertionError("pooled constructor should be used")

    with (
        patch.object(short_term, "_global_checkpointer", None),
        patch.object(short_term, "_pg_saver_cm", None),
        patch.object(short_term, "_pg_async_pool", None),
        patch.object(short_term, "get_settings", return_value=cfg),
        patch("psycopg_pool.AsyncConnectionPool", return_value=pool),
        patch("langgraph.checkpoint.postgres.aio.AsyncPostgresSaver", FakeAsyncPostgresSaver),
    ):
        initialized = await short_term.initialize_checkpointer()
        assert initialized is created_savers[0]
        pool.open.assert_awaited_once()
        created_savers[0].setup.assert_awaited_once()

    class BrokenSaver:
        @staticmethod
        def from_conn_string(_uri: str) -> object:
            raise OSError("database down")

    with (
        patch.object(short_term, "_global_checkpointer", None),
        patch.object(short_term, "get_settings", return_value=cfg),
        patch.object(short_term, "is_strict_env", return_value=True),
        patch("psycopg_pool.AsyncConnectionPool", side_effect=ImportError("unavailable")),
        patch("langgraph.checkpoint.postgres.aio.AsyncPostgresSaver", BrokenSaver),
        pytest.raises(RuntimeError, match="PostgreSQL checkpointer initialization failed"),
    ):
        await short_term.initialize_checkpointer()


@pytest.mark.asyncio
async def test_thread_registry_duplicate_keys_and_fallback_failures() -> None:
    cfg = SimpleNamespace(
        environment="development",
        checkpoint_signing_key="active-key",
        checkpoint_signing_keys=("same-key", "same-key"),
        redis_state_url=None,
        redis_url="redis://unavailable:6379/0",
    )
    with patch("src.agentic.memory.thread_registry.get_settings", return_value=cfg):
        registry = ThreadRegistry()
    assert len(registry.candidate_scoped_thread_ids("thread", "owner", "tenant")) == 1

    redis_client = SimpleNamespace(ping=AsyncMock(side_effect=OSError("offline")))
    with patch("redis.asyncio.from_url", return_value=redis_client):
        assert await registry._client() is None

    closing_client = SimpleNamespace(aclose=AsyncMock(side_effect=OSError("close failed")))
    registry._redis = closing_client
    await registry._handle_redis_failure("claim", closing_client, OSError("claim failed"))
    assert registry._redis is None


@pytest.mark.asyncio
async def test_thread_registry_owner_none_and_forget_local_branches() -> None:
    cfg = SimpleNamespace(
        environment="development",
        checkpoint_signing_key="active-key",
        checkpoint_signing_keys=("active-key",),
        redis_state_url=None,
        redis_url="redis://unused:6379/0",
    )
    with patch("src.agentic.memory.thread_registry.get_settings", return_value=cfg):
        registry = ThreadRegistry()
    client = SimpleNamespace(get=AsyncMock(return_value=None), aclose=AsyncMock())
    registry._redis = client
    assert await registry.owner("missing", "tenant") is None

    registry_id = registry._registry_id("thread-a", "tenant")
    registry._owners = {registry_id: "alice", "stale": "alice", "other": "bob"}
    registry._owner_threads = {"alice": {registry_id, "missing"}, "bob": {"other"}}
    await registry.forget_local(owner_id="alice", thread_ids={registry_id})
    assert "alice" not in registry._owner_threads
    assert all(owner != "alice" for owner in registry._owners.values())
    assert registry._owners["other"] == "bob"
    await registry.aclose()
    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_security_body_protocol_edges() -> None:
    app = AsyncMock()
    with patch(
        "src.agentic.tools.servers.security.get_settings",
        return_value=Settings(environment="test"),
    ):
        middleware = MCPServiceAuthMiddleware(app)

    receive = AsyncMock(
        side_effect=[
            {"type": "http.disconnect"},
            {"type": "http.request", "body": b"fallback", "more_body": False},
        ]
    )
    body, replay = await middleware._read_body(receive)
    assert body == b""
    assert await replay() == {"type": "http.disconnect"}
    assert (await replay())["body"] == b"fallback"

    send = AsyncMock()
    assert await middleware._authorize_tool_call({}, b"", send) is True
    assert await middleware._authorize_tool_call({}, b"{broken", send) is True


def test_mcp_server_transport_factories() -> None:
    stdio = SimpleNamespace(run=MagicMock())
    run_mcp_server(stdio, transport="stdio", port=9000)
    stdio.run.assert_called_once()

    fallback_app = object()
    factory = MagicMock(side_effect=[TypeError("old API"), fallback_app])
    mcp = SimpleNamespace(streamable_http_app=factory)
    with patch("uvicorn.run") as uvicorn_run:
        run_mcp_server(mcp, transport="streamable-http", port=9001)
    assert factory.call_count == 2
    assert uvicorn_run.call_args.kwargs == {"host": "0.0.0.0", "port": 9001}

    sse_app = object()
    mcp = SimpleNamespace(sse_app=MagicMock(return_value=sse_app))
    with patch("uvicorn.run") as uvicorn_run:
        run_mcp_server(mcp, transport="streamable-http", port=9002)
    assert uvicorn_run.called

    with pytest.raises(ValueError, match="Unsupported MCP transport"):
        run_mcp_server(SimpleNamespace(), transport="invalid", port=9003)
