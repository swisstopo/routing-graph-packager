import os
from typing import Generator

from sqlalchemy.exc import ProgrammingError
from sqlmodel import create_engine, Session, SQLModel

from .config import SETTINGS as S

SQLALCHEMY_DATABASE_URI: str = f"postgresql://{S.POSTGRES_USER}:{S.POSTGRES_PASS}@{S.POSTGRES_HOST}:{S.POSTGRES_PORT}/{S.POSTGRES_DB}"

engine = create_engine(SQLALCHEMY_DATABASE_URI, echo=bool(os.getenv("DEBUG")), future=True)


def create_tables() -> None:
    """
    Creates every table that does not exist yet.

    All three containers call this, not just the app: the worker and the graph builder both need
    ``graph_locks`` before they can take a lock, and neither waits for the app to have started.
    Two of them starting at once can collide on the same ``CREATE``, which is harmless as long as
    the table exists afterwards.
    """
    from .api_v1 import models  # noqa: F401

    try:
        SQLModel.metadata.create_all(engine, checkfirst=True)
    except ProgrammingError:
        SQLModel.metadata.create_all(engine, checkfirst=True)


def get_db() -> Generator[Session, None, None]:
    """Gets a DB Session."""
    db = Session(engine, autocommit=False, autoflush=False)
    try:
        yield db
    finally:
        db.close()
