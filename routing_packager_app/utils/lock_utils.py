"""
Fences the graph build against the packaging jobs with rows in ``graph_locks``.

The builder and the workers coordinate over two things only: the ``graph`` symlink, which says which
generation is current, and the locks in here, which say which generations are being read. Neither
needs the two to share a machine, so the builder can run wherever it has the shared volume and the
database in reach.

A lock is a row, not a connection. Every operation here is a short transaction: a build that runs for
hours holds nothing open, and the lock survives a database restart, a dropped connection and the death
of the process that took it. What keeps a dead holder from blocking a resource forever is the lease —
every row carries an ``expires_at`` of ``GRAPH_LOCK_TTL`` seconds, and expired rows are deleted by the
next acquire. Nothing renews a lease, so ``GRAPH_LOCK_TTL`` has to be shorter than the interval between
builds, and the one real cost of the design is that a crashed holder blocks its resource until the
lease runs out. A stuck lock is visible in ``SELECT * FROM graph_locks`` and cleared with a ``DELETE``.

Every timestamp comes from Postgres rather than from Python, because the builder's clock and the
workers' are not the same clock.
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
SHARED_LOCK_TIMEOUT = 300.0
RESOLVE_ATTEMPTS = 2


def lock_path(path: Path) -> str:
    """
    Turns a directory into the string identifying its lock.

    The path is stored relative to ``TMP_DATA_DIR`` so that two processes mounting the volume in
    different places still name the same directory the same way. An absolute path would make the
    builder's ``/app/tmp_data/osm`` and a worker's ``/srv/tmp_data/osm`` look like two locks, and the
    fence would silently never engage.

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

    The table lock is what makes this atomic. Looking for a conflict and then inserting are two
    statements, and without serialising them a shared and an exclusive acquire can both find nothing
    and both insert, which is the pruner deleting a directory out from under a running packaging job.
    It is held for the few milliseconds this transaction lasts, over a table with a handful of rows.

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

    Resolving and locking are two separate steps, so the generation can in principle be pruned in
    between. Re-resolving the symlink afterwards is what rules that out: if it still points at the
    generation we locked, that generation is the current one and therefore never a prune candidate.
    If it moved, a whole build finished in the gap, and resolving again lands on its result.

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
