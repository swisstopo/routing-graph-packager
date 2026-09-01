import hashlib
import os
from typing import Generator

from sqlalchemy import text
from sqlmodel import create_engine, Session, SQLModel

from .config import SETTINGS as S

SQLALCHEMY_DATABASE_URI: str = f"postgresql://{S.POSTGRES_USER}:{S.POSTGRES_PASS}@{S.POSTGRES_HOST}:{S.POSTGRES_PORT}/{S.POSTGRES_DB}"

engine = create_engine(SQLALCHEMY_DATABASE_URI, echo=bool(os.getenv("DEBUG")), future=True)

DDL_LOCK_KEY = int.from_bytes(
    hashlib.blake2b(b"rgp:create_tables", digest_size=8).digest(), "big", signed=True
)


def create_tables() -> None:
    """
    Creates every table that does not exist yet.

    All three containers call this, not just the app: the worker and the graph builder both need
    ``graph_locks`` before they can take a lock, and neither waits for the app to have started.

    pg_advisory_xact_lock is used to avoid data races when multiple services try to create the tables
    at the same time.

    """

    # SQLModel creates all tables in scope automatically
    from .api_v1 import models  # noqa: F401

    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": DDL_LOCK_KEY})
        SQLModel.metadata.create_all(conn, checkfirst=True)


def get_db() -> Generator[Session, None, None]:
    """Gets a DB Session."""
    db = Session(engine, autocommit=False, autoflush=False)
    try:
        yield db
    finally:
        db.close()
