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

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)
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
