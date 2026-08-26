import os
from typing import Generator

from sqlalchemy.pool import NullPool
from sqlmodel import create_engine, Session

from .config import SETTINGS as S

SQLALCHEMY_DATABASE_URI: str = f"postgresql://{S.POSTGRES_USER}:{S.POSTGRES_PASS}@{S.POSTGRES_HOST}:{S.POSTGRES_PORT}/{S.POSTGRES_DB}"

KEEPALIVE_ARGS = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 5,
}

engine = create_engine(SQLALCHEMY_DATABASE_URI, echo=bool(os.getenv("DEBUG")), future=True)

lock_engine = create_engine(
    SQLALCHEMY_DATABASE_URI,
    echo=bool(os.getenv("DEBUG")),
    future=True,
    poolclass=NullPool,
    connect_args=KEEPALIVE_ARGS,
)


def get_db() -> Generator[Session, None, None]:
    """Gets a DB Session."""
    db = Session(engine, autocommit=False, autoflush=False)
    try:
        yield db
    finally:
        db.close()
