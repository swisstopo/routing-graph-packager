"""
Protects ongoing packaging jobs against a new graph build with rows in ``graph_locks``.

The builder and the workers coordinate over two things only: the ``graph`` symlink, which says which
generation is current, and the locks in here, which say which generations are being read.
"""

import os
import socket
import time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Iterator

from sqlalchemy import DateTime, Interval, delete, func, insert, literal, select, text

from ..api_v1.models import GraphLock
from ..config import SETTINGS
from ..constants import LockMode
from ..db import engine

LOCK_POLL_INTERVAL = 5.0
SHARED_LOCK_TIMEOUT = 300.0  # 5 minutes
RESOLVE_ATTEMPTS = 2


def lock_path(path: Path) -> str:
    """
    Turns a directory into the string identifying its lock.

    The path is stored relative to ``TMP_DATA_DIR`` so that two processes mounting the volume in
    different places still name the same directory the same way.

    :param path: the directory being locked.

    :raises ValueError: if the path is not below ``TMP_DATA_DIR``.
    """
    return str(path.relative_to(SETTINGS.get_tmp_data_dir()))


def _holder() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _utc_now():
    return func.timezone("utc", func.now(), type_=DateTime())


def _try_acquire(path: str, mode: LockMode) -> int | None:
    """
    Takes a lock on a path, or reports that somebody else holds it.

    This is a single atomic operation by means of an exclusive lock on the graph locks table.

    :param path: the lock's path, from :func:`lock_path`.
    :param mode: shared locks admit each other, an exclusive lock admits nobody.

    :returns: the id of the row taken, or ``None`` if the lock is held.
    """
    with engine.begin() as conn:
        conn.execute(text(f"LOCK TABLE {GraphLock.__tablename__} IN EXCLUSIVE MODE"))
        conn.execute(delete(GraphLock).where(GraphLock.expires_at <= _utc_now()))

        conflicts = select(GraphLock.id).where(GraphLock.path == path)
        if mode is LockMode.SHARED:
            conflicts = conflicts.where(GraphLock.mode == LockMode.EXCLUSIVE)
        if conn.execute(conflicts.limit(1)).first():
            return None

        return conn.execute(
            insert(GraphLock)
            .values(
                path=path,
                mode=mode,
                holder=_holder(),
                acquired_at=_utc_now(),
                expires_at=_utc_now() + literal(timedelta(seconds=SETTINGS.GRAPH_LOCK_TTL), Interval()),
            )
            .returning(GraphLock.id)
        ).scalar_one()


def _release(lock_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(delete(GraphLock).where(GraphLock.id == lock_id))


def _acquire(path: str, mode: LockMode, timeout: float) -> int | None:
    """
    Keeps trying to take a lock until it is granted or the timeout is up.

    :param path: the lock's path, from :func:`lock_path`.
    :param mode: shared locks admit each other, an exclusive lock admits nobody.
    :param timeout: how many seconds to keep retrying for. Zero means a single attempt.

    :returns: the id of the row taken, or ``None`` if it was never granted.
    """
    deadline = time.monotonic() + timeout
    while True:
        lock_id = _try_acquire(path, mode)
        if lock_id is not None:
            return lock_id

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None

        time.sleep(min(LOCK_POLL_INTERVAL, remaining))


@contextmanager
def lock_exclusive(path: Path, timeout: float = 0.0) -> Iterator[bool]:
    """
    Takes an exclusive lock on a directory, excluding every other holder of the same path.

    :param path: the directory to lock, below ``TMP_DATA_DIR``.
    :param timeout: how many seconds to keep retrying for. Zero means a single attempt.

    :returns: whether the lock was acquired.
    """
    name = lock_path(path)
    lock_id = None
    try:
        lock_id = _acquire(name, LockMode.EXCLUSIVE, timeout)
        yield lock_id is not None
    finally:
        if lock_id is not None:
            _release(lock_id)


@contextmanager
def lock_generation_shared(link: Path, timeout: float = SHARED_LOCK_TIMEOUT) -> Iterator[Path]:
    """
    Resolves the graph symlink and holds a shared lock on the generation it points at.

    Several workers can hold this at once; only the pruner, which needs the path exclusively before
    deleting, is kept out. Holding it guarantees the resolved directory survives for as long as the
    caller reads from it.

    :param link: the graph symlink, e.g. tmp_data/osm/graph.
    :param timeout: how many seconds to wait for a pruner to release the generation.

    :returns: the resolved generation directory.

    :raises OSError: if no generation could be locked.
    """
    error: OSError | None = None

    for _ in range(RESOLVE_ATTEMPTS):
        try:
            generation = link.resolve(strict=True)
        except OSError as e:
            error = e
            continue

        lock_id = None
        try:
            lock_id = _acquire(lock_path(generation), LockMode.SHARED, timeout)
            if lock_id is None:
                error = TimeoutError(
                    f"Graph generation {generation.name} was still held by the graph build after "
                    f"{timeout:.0f}s."
                )
                continue

            if not _is_current(link, generation):
                error = FileNotFoundError(
                    f"Graph generation {generation.name} was replaced while locking it."
                )
                continue

            yield generation
        finally:
            if lock_id is not None:
                _release(lock_id)

        return

    raise error or FileNotFoundError(f"No graph generation behind {link}.")


def _is_current(link: Path, generation: Path) -> bool:
    try:
        return link.resolve(strict=True) == generation
    except OSError:
        return False
