"""Programmatic database migration management using Alembic."""

import logging

from alembic import command
from alembic.config import Config

from src.agentic.core.config import get_settings

logger = logging.getLogger(__name__)


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


def run_database_migrations(db_uri: str | None = None) -> bool:
    """Executes all pending Alembic database migrations up to head."""
    settings = get_settings()
    try:
        cfg = get_alembic_config(db_uri)
        logger.info(f"Applying database migrations to head on {settings.postgres_uri}...")
        command.upgrade(cfg, "head")
        logger.info("Database schema successfully upgraded to head revision.")
        return True
    except Exception as e:
        logger.warning(f"Could not apply database migrations: {e}")
        return False
