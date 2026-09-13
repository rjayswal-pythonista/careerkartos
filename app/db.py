"""Database engine/session setup.

Postgres in production; SQLite is supported so the pipeline can be run and tested
locally with no infrastructure. Set DATABASE_URL to switch.
"""

import os
from contextlib import contextmanager

import logging

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from .models.schema import Base

log = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./jobs.db")

if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}
    engine_kwargs = {}
else:
    # Postgres. Small instances drop idle connections and refuse new ones under
    # load, so verify liveness on checkout rather than handing out a dead socket,
    # recycle before the server's own timeout, and keep the pool small — the
    # scrape is I/O-bound on remote APIs, not on the database.
    connect_args = {"connect_timeout": 10}
    engine_kwargs = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
        "pool_size": 5,
        "max_overflow": 2,
        "pool_timeout": 30,
    }

engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db():
    Base.metadata.create_all(engine)
    _add_missing_columns()
    _backfill_company_name()
    _ensure_search_index()


def _add_missing_columns():
    """Add columns introduced after a database was first created.

    create_all() creates missing *tables* but never alters existing ones, so a
    new column is invisible to any database that already exists — including
    production. This project has no migration tool; ADD COLUMN IF NOT EXISTS is
    the whole requirement so far, and is safe to run on every start.
    """
    stmts = {
        "postgresql": ["ALTER TABLE jobs ADD COLUMN IF NOT EXISTS company_name VARCHAR(200)"],
        "sqlite": ["ALTER TABLE jobs ADD COLUMN company_name VARCHAR(200)"],
    }.get(engine.dialect.name, [])
    for ddl in stmts:
        try:
            with engine.begin() as conn:
                conn.execute(text(ddl))
        except Exception:
            # SQLite has no IF NOT EXISTS for columns; a duplicate is expected
            # and harmless on every run after the first.
            pass


def _backfill_company_name():
    """Populate jobs.company_name for rows written before it existed.

    The column feeds the full-text vector, so a null leaves those rows
    unsearchable by company. Cheap and idempotent: it only touches rows where
    the value is still missing.
    """
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE jobs SET company_name = companies.name "
                "FROM companies WHERE companies.id = jobs.company_id "
                "AND jobs.company_name IS DISTINCT FROM companies.name"
            ) if engine.dialect.name == "postgresql" else text(
                "UPDATE jobs SET company_name = ("
                "SELECT name FROM companies WHERE companies.id = jobs.company_id) "
                "WHERE company_name IS NULL"
            ))
    except Exception as e:  # pragma: no cover
        log.warning("company_name backfill skipped: %s", e)


def _ensure_search_index():
    """Create the full-text index Postgres search depends on.

    Without it the tsvector match still works but falls back to recomputing the
    vector per row, which is slower than the LIKE it replaced. SQLite uses the
    LIKE path and needs nothing here.

    Built as a plain (non-CONCURRENT) index because it runs inside the same
    startup path as create_all; on a table of this size it is seconds, and it
    is skipped entirely once present.
    """
    if engine.dialect.name != "postgresql":
        return
    ddl = text(
        "CREATE INDEX IF NOT EXISTS ix_jobs_fts ON jobs USING GIN ("
        "to_tsvector('english',"
        " coalesce(title,'') || ' ' || coalesce(normalized_title,'')"
        " || ' ' || coalesce(department,'') || ' ' || coalesce(company_name,'')"
        " || ' ' || coalesce(description_raw,'')))"
    )
    try:
        with engine.begin() as conn:
            conn.execute(ddl)
        log.info("full-text search index present")
    except Exception as e:  # pragma: no cover - index is an optimisation
        # Deliberately non-fatal: search still returns correct results without
        # it, just by scanning. But the failure is otherwise invisible, so it is
        # logged loudly and reported by /api/health ("search_index": false).
        log.error(
            "FULL-TEXT INDEX MISSING — search will scan the whole table. "
            "Re-run init_db() once the schema is settled. Cause: %s", e
        )


@contextmanager
def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def session_factory():
    """Context-manager factory matching what Orchestrator expects."""
    return SessionLocal()
