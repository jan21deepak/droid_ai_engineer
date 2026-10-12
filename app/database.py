"""Database engine, session factory, initialization and lightweight migrations."""

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

import app.delivery_models
import app.run_models  # noqa: F401  (registers run_attempts on Base.metadata)
from app.config import get_settings
from app.logging_conf import log_event
from app.models import Base

logger = logging.getLogger("app.database")

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        url = settings.database_url
        if url.startswith("sqlite:///"):
            path = url.replace("sqlite:///", "", 1)
            if path and not path.startswith(":memory:"):
                directory = os.path.dirname(os.path.abspath(path))
                os.makedirs(directory, exist_ok=True)
        _engine = create_engine(url, connect_args={"check_same_thread": False})
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionLocal


@contextmanager
def db_session() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _run_lightweight_migrations(engine: Engine) -> None:
    """Add any columns present in models but missing from existing tables.

    Additive and idempotent: existing rows and columns are never rewritten, and
    added columns get no value on old rows (ORM defaults apply to new inserts
    only), so unknown history stays unknown. New tables come from ``create_all``.
    """
    inspector = inspect(engine)
    for table_name, table in Base.metadata.tables.items():
        if table_name not in inspector.get_table_names():
            continue
        existing = {col["name"] for col in inspector.get_columns(table_name)}
        for column in table.columns:
            if column.name not in existing:
                col_type = column.type.compile(engine.dialect)
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column.name} {col_type}"))
                log_event(
                    logger,
                    logging.INFO,
                    "db.migration_applied",
                    table=table_name,
                    column=column.name,
                )


def init_db() -> None:
    engine = get_engine()
    _run_lightweight_migrations(engine)
    Base.metadata.create_all(engine)
    log_event(logger, logging.INFO, "db.initialized", url=get_settings().database_url)


def check_db_connectivity() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # pragma: no cover - defensive
        log_event(logger, logging.ERROR, "db.connectivity_failed", error=str(exc))
        return False


def reset_for_tests() -> None:
    """Reset cached engine/session factory (used by the test suite)."""
    global _engine, _SessionLocal
    _engine = None
    _SessionLocal = None
