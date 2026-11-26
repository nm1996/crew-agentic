"""Engine/session factory and programmatic Alembic migrations."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def sqlite_url(db_file: Path) -> str:
    return f"sqlite:///{db_file}"


def make_engine(db_file: Path) -> Engine:
    engine = create_engine(sqlite_url(db_file), connect_args={"timeout": 30})

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        # WAL keeps the DB consistent if CREW is killed mid-write and lets
        # `crew status` read while a run is executing.
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def _alembic_config(db_file: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", sqlite_url(db_file))
    return cfg


def migrate(db_file: Path) -> None:
    """Create or upgrade the database schema to the latest revision."""
    db_file.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(_alembic_config(db_file), "head")


def schema_status(db_file: Path) -> tuple[str | None, str | None]:
    """Return (current_revision, head_revision)."""
    head = ScriptDirectory.from_config(_alembic_config(db_file)).get_current_head()
    if not db_file.exists():
        return None, head
    engine = make_engine(db_file)
    try:
        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()
    return current, head
