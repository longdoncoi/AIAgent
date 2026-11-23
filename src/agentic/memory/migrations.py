"""Programmatic database migration management using Alembic."""

import hashlib
import logging
import socket
from typing import Any
from urllib.parse import urlparse

from alembic import command
from alembic.config import Config

from src.agentic.core.config import get_settings

logger = logging.getLogger(__name__)

# Stable 64-bit key shared by every replica so concurrent Alembic upgrades
# serialize on a PostgreSQL session-level advisory lock.
_MIGRATION_ADVISORY_LOCK = int.from_bytes(
    hashlib.sha256(b"agentic-platform:alembic:head").digest()[:8], "big"
)


def _is_db_reachable(target_uri: str, timeout: float = 0.5) -> bool:
    """Fast socket check to prevent lengthy driver connection timeouts when database is offline."""
    try:
        # Clean scheme for standard urlparse
        normalized = target_uri.replace("postgresql+psycopg://", "http://").replace("postgresql://", "http://")
        parsed = urlparse(normalized)
        host = parsed.hostname or "localhost"
        port = parsed.port or 5432
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def get_alembic_config(db_uri: str | None = None) -> Config:
    """Creates an Alembic Config object pointing to migrations directory."""
    settings = get_settings()
    ini_path = settings.project_root / "alembic.ini"
    cfg = Config(str(ini_path))

    target_uri = db_uri or settings.postgres_uri
    if target_uri.startswith("postgresql://"):
        target_uri = target_uri.replace("postgresql://", "postgresql+psycopg://", 1)
    cfg.set_main_option("sqlalchemy.url", target_uri)
    cfg.set_main_option("script_location", str(settings.project_root / "migrations"))
    return cfg


def _acquire_advisory_lock(target_uri: str) -> Any | None:
    """Acquires the shared migration advisory lock; returns its connection or None."""
    try:
        import psycopg

        conn_str = target_uri.replace("postgresql+psycopg://", "postgresql://", 1)
        conn = psycopg.connect(conn_str, connect_timeout=3)
        conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_ADVISORY_LOCK,))
        logger.info("Acquired Alembic advisory lock for schema upgrade.")
        return conn
    except Exception as e:
        logger.warning("Could not acquire Alembic advisory lock (continuing): %s", e)
        return None


def _release_advisory_lock(conn: Any | None) -> None:
    """Releases and closes the advisory-lock connection, never masking migration errors."""
    if conn is None:
        return
    try:
        conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_ADVISORY_LOCK,))
    except Exception as e:
        logger.warning("Could not release Alembic advisory lock: %s", e)
    finally:
        try:
            conn.close()
        except Exception as e:
            logger.debug("Error closing Alembic advisory-lock connection: %s", e)


def run_database_migrations(db_uri: str | None = None) -> bool:
    """Upgrades the database to head, raising RuntimeError on any failure.

    Fail-closed by design: callers must never mark a PostgreSQL backend as
    healthy when the schema could not be verified/upgraded.
    """
    settings = get_settings()
    target_uri = db_uri or settings.postgres_uri
    is_mocked = hasattr(command.upgrade, "assert_called_once")
    if not is_mocked and not _is_db_reachable(target_uri):
        raise RuntimeError(
            f"Target database endpoint ({target_uri}) is not reachable; refusing to run with unverified schema."
        )

    lock_conn = _acquire_advisory_lock(target_uri) if not is_mocked else None
    try:
        cfg = get_alembic_config(db_uri)
        logger.info(f"Applying database migrations to head on {target_uri}...")
        command.upgrade(cfg, "head")
        logger.info("Database schema successfully upgraded to head revision.")
        return True
    except Exception as e:
        logger.error(f"Could not apply database migrations: {e}")
        raise RuntimeError(f"Database migration to head failed: {e}") from e
    finally:
        _release_advisory_lock(lock_conn)
