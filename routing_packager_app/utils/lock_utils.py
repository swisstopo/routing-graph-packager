"""
Fences the graph build against the packaging jobs using Postgres advisory locks.

The builder and the workers coordinate over two things only: the ``graph`` symlink, which says
which generation is current, and the locks in here, which say which generations are being read.
Neither needs the two to share a machine, so the builder can run wherever it has the shared volume
and the database in reach.

Advisory locks are held by the Postgres *session* that took them, which gives the three properties
this design needs: acquiring is atomic, several workers can hold the same generation at once while
a pruner cannot, and a lock is released on its own when its holder dies. The price is that the
connection has to stay alive for as long as the lock is held — hours, for a planet build — which is
what ``db.lock_engine`` and its keepalives are for.

Every statement here commits immediately. A session-level lock does not need a transaction, and a
connection left open in one for the length of a build would pin the xmin horizon and keep autovacuum
from cleaning up dead tuples across the whole database.
"""

import hashlib
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Connection, text

from ..db import lock_engine

LOCK_POLL_INTERVAL = 5.0
SHARED_LOCK_TIMEOUT = 300.0
RESOLVE_ATTEMPTS = 2

TRY_EXCLUSIVE = "pg_try_advisory_lock"
TRY_SHARED = "pg_try_advisory_lock_shared"
UNLOCK_EXCLUSIVE = "pg_advisory_unlock"
UNLOCK_SHARED = "pg_advisory_unlock_shared"


def build_lock_name(provider: str) -> str:
    """
    Names the lock that admits a single graph build at a time.

    :param provider: the dataset provider being built.
    """
    return f"rgp:build:{provider}"


def generation_lock_name(provider: str, generation: str) -> str:
    """
    Names the lock that keeps a generation from being pruned while it is read.

    :param provider: the dataset provider the generation belongs to.
    :param generation: the generation's directory name.
    """
    return f"rgp:generation:{provider}:{generation}"


def lock_key(name: str) -> int:
    """
    Turns a lock name into bigint (used by Postgres for advisory locks).

    Python salts per `hash` per process, so this has to be used instead.

    :param name: a lock name.
    """
    digest = hashlib.blake2b(name.encode("utf8"), digest_size=8).digest()

    return int.from_bytes(digest, "big", signed=True)


def _call(conn: Connection, function: str, key: int) -> bool:
    result = conn.execute(text(f"SELECT {function}(:key)"), {"key": key}).scalar()
    conn.commit()

    return bool(result)


def _acquire(conn: Connection, function: str, key: int, timeout: float) -> bool:
    """
    Keeps trying to take a lock until it is granted or the timeout is up.

    Polling with the ``try`` variants rather than blocking inside Postgres keeps the wait
    interruptible and bounded, which matters for a build that would otherwise sit in the database
    with no way out.

    :param conn: the connection that will hold the lock.
    :param function: the Postgres function to acquire with.
    :param key: the lock's key.
    :param timeout: how many seconds to keep retrying for. Zero means a single attempt.

    :returns: whether the lock was acquired.
    """
    deadline = time.monotonic() + timeout
    while True:
        if _call(conn, function, key):
            return True

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False

        time.sleep(min(LOCK_POLL_INTERVAL, remaining))


@contextmanager
def lock_exclusive(name: str, timeout: float = 0.0) -> Iterator[bool]:
    """
    Takes an exclusive advisory lock, excluding every other holder of the same name.

    :param name: the lock's name, from :func:`build_lock_name` or :func:`generation_lock_name`.
    :param timeout: how many seconds to keep retrying for. Zero means a single attempt.

    :returns: whether the lock was acquired.
    """
    key = lock_key(name)
    conn = lock_engine.connect()
    acquired = False
    try:
        acquired = _acquire(conn, TRY_EXCLUSIVE, key, timeout)
        yield acquired
    finally:
        if acquired:
            _call(conn, UNLOCK_EXCLUSIVE, key)
        conn.close()


@contextmanager
def lock_generation_shared(link: Path, timeout: float = SHARED_LOCK_TIMEOUT) -> Iterator[Path]:
    """
    Resolves the graph symlink and holds a shared lock on the generation it points at.

    Several workers can hold this at once; only the pruner, which needs the lock exclusively before
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
    provider = link.parent.name
    error: OSError | None = None

    for _ in range(RESOLVE_ATTEMPTS):
        try:
            generation = link.resolve(strict=True)
        except OSError as e:
            error = e
            continue

        key = lock_key(generation_lock_name(provider, generation.name))
        conn = lock_engine.connect()
        acquired = False
        try:
            acquired = _acquire(conn, TRY_SHARED, key, timeout)
            if not acquired:
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
            if acquired:
                _call(conn, UNLOCK_SHARED, key)
            conn.close()

        return

    raise error or FileNotFoundError(f"No graph generation behind {link}.")


def _is_current(link: Path, generation: Path) -> bool:
    try:
        return link.resolve(strict=True) == generation
    except OSError:
        return False
