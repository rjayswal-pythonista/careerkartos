"""Database engine/session setup.

Postgres in production; SQLite is supported so the pipeline can be run and tested
locally with no infrastructure. Set DATABASE_URL to switch.
"""

import os
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .models.schema import Base

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
