import os
import socket
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Interval, insert, literal, select

from routing_packager_app.api_v1.models import GraphLock
from routing_packager_app.config import SETTINGS
from routing_packager_app.constants import LockMode
from routing_packager_app.db import engine
from routing_packager_app.graph_build.builder import swap_graph_link
from routing_packager_app.utils import lock_utils
from tests.utils_ import PROVIDER
from routing_packager_app.utils.lock_utils import (
    lock_exclusive,
    lock_generation_shared,
    lock_path,
)


@pytest.fixture
def provider_dir():
    return SETTINGS.get_provider_dir(PROVIDER)


@pytest.fixture
def graph(graph_dirs):
    generations, link = graph_dirs

    def make(generation_name):
        generation = generations.joinpath(generation_name)
        generation.mkdir()
        generation.joinpath("tile.gph").write_bytes(b"tile")

        return generation

    return link, make


def hold(path, mode=LockMode.EXCLUSIVE, ttl=None):
    """Takes a lock on a connection that is closed again before this returns."""
    if ttl is None:
        ttl = SETTINGS.GRAPH_LOCK_TTL

    conn = engine.connect()
    try:
        lock_id = conn.execute(
            insert(GraphLock)
            .values(
                path=lock_path(path),
                mode=mode,
                holder="test",
                acquired_at=lock_utils._utc_now(),
                expires_at=lock_utils._utc_now() + literal(timedelta(seconds=ttl), Interval()),
            )
            .returning(GraphLock.id)
        ).scalar_one()
        conn.commit()

        return lock_id
    finally:
        conn.close()


def rows(path=None):
    statement = select(GraphLock)
    if path is not None:
        statement = statement.where(GraphLock.path == lock_path(path))

    with engine.connect() as conn:
        return conn.execute(statement).all()


def test_exclusive_is_granted_when_nobody_holds_it(provider_dir):
    with lock_exclusive(provider_dir) as acquired:
        assert acquired is True


def test_exclusive_excludes_another_exclusive(provider_dir):
    hold(provider_dir)

    with lock_exclusive(provider_dir) as acquired:
        assert acquired is False


def test_exclusive_is_released_on_exit(provider_dir):
    with lock_exclusive(provider_dir) as acquired:
        assert acquired is True

    assert rows(provider_dir) == []
    with lock_exclusive(provider_dir) as acquired:
        assert acquired is True


def test_exclusive_is_released_when_the_body_raises(provider_dir):
    with pytest.raises(ValueError):
        with lock_exclusive(provider_dir):
            raise ValueError("boom")

    with lock_exclusive(provider_dir) as acquired:
        assert acquired is True


def test_shared_does_not_exclude_shared(graph):
    link, make = graph
    generation = make("20260101T000000")
    swap_graph_link(link, generation)

    with lock_generation_shared(link) as first:
        with lock_generation_shared(link) as second:
            assert first == second == generation
            assert len(rows(generation)) == 2


def test_exclusive_excludes_a_shared_holder(graph):
    _, make = graph
    generation = make("20260101T000000")

    lock_id = hold(generation, LockMode.SHARED)
    with lock_exclusive(generation) as acquired:
        assert acquired is False

    lock_utils._release(lock_id)
    with lock_exclusive(generation) as acquired:
        assert acquired is True


def test_shared_is_excluded_by_an_exclusive_holder(graph):
    link, make = graph
    generation = make("20260101T000000")
    swap_graph_link(link, generation)
    hold(generation, LockMode.EXCLUSIVE)

    with pytest.raises(OSError, match="still held by the graph build"):
        with lock_generation_shared(link, timeout=0.0):
            pass


def test_exclusive_waits_out_a_shared_holder(graph, monkeypatch):
    monkeypatch.setattr(lock_utils, "LOCK_POLL_INTERVAL", 0.05)
    _, make = graph
    generation = make("20260101T000000")

    lock_id = hold(generation, LockMode.SHARED)
    threading.Timer(0.3, lock_utils._release, [lock_id]).start()

    with lock_exclusive(generation, timeout=30) as acquired:
        assert acquired is True


def test_exclusive_gives_up_after_the_timeout(graph, monkeypatch):
    monkeypatch.setattr(lock_utils, "LOCK_POLL_INTERVAL", 0.05)
    _, make = graph
    generation = make("20260101T000000")
    hold(generation, LockMode.SHARED)

    with lock_exclusive(generation, timeout=0.2) as acquired:
        assert acquired is False


def test_a_lock_outlives_the_connection_that_took_it(provider_dir):
    hold(provider_dir)

    assert len(rows(provider_dir)) == 1
    with lock_exclusive(provider_dir) as acquired:
        assert acquired is False


def test_an_expired_lock_does_not_block(provider_dir):
    hold(provider_dir, ttl=-1)

    with lock_exclusive(provider_dir) as acquired:
        assert acquired is True


def test_acquiring_reaps_every_expired_row(provider_dir, graph):
    _, make = graph
    generation = make("20260101T000000")
    hold(generation, LockMode.SHARED, ttl=-1)

    with lock_exclusive(provider_dir):
        pass

    assert rows(generation) == []


def test_locks_on_different_paths_do_not_interfere(provider_dir, graph):
    _, make = graph
    generation = make("20260101T000000")
    hold(generation)

    with lock_exclusive(provider_dir) as acquired:
        assert acquired is True


def test_only_one_of_two_racing_exclusives_wins(provider_dir):
    name = lock_path(provider_dir)
    barrier = threading.Barrier(2)
    won = []

    def race():
        barrier.wait()
        won.append(lock_utils._try_acquire(name, LockMode.EXCLUSIVE))

    threads = [threading.Thread(target=race) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len([lock_id for lock_id in won if lock_id is not None]) == 1


def test_a_racing_shared_and_exclusive_cannot_both_win(graph):
    _, make = graph
    name = lock_path(make("20260101T000000"))
    barrier = threading.Barrier(2)
    won = []

    def race(mode):
        barrier.wait()
        won.append(lock_utils._try_acquire(name, mode))

    threads = [
        threading.Thread(target=race, args=(mode,)) for mode in (LockMode.SHARED, LockMode.EXCLUSIVE)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len([lock_id for lock_id in won if lock_id is not None]) == 1


def test_the_lease_lasts_the_configured_ttl(provider_dir):
    with lock_exclusive(provider_dir):
        row = rows(provider_dir)[0]

    assert row.expires_at - row.acquired_at == timedelta(seconds=SETTINGS.GRAPH_LOCK_TTL)


def test_the_lease_is_stamped_in_utc(provider_dir):
    with lock_exclusive(provider_dir):
        row = rows(provider_dir)[0]

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    assert abs(row.acquired_at - now) < timedelta(minutes=1)


def test_the_holder_records_host_and_pid(provider_dir):
    with lock_exclusive(provider_dir):
        row = rows(provider_dir)[0]

    assert row.holder == f"{socket.gethostname()}:{os.getpid()}"


def test_lock_path_is_relative_to_the_data_dir(graph):
    _, make = graph
    generation = make("20260101T000000")

    assert lock_path(generation) == f"osm/generations/{generation.name}"


def test_lock_path_rejects_a_path_outside_the_data_dir(tmp_path):
    with pytest.raises(ValueError):
        lock_path(tmp_path)


def test_lock_generation_shared_yields_the_current_generation(graph):
    link, make = graph
    generation = make("20260101T000000")
    swap_graph_link(link, generation)

    with lock_generation_shared(link) as resolved:
        assert resolved == generation


def test_lock_generation_shared_without_a_graph(graph):
    link, _ = graph

    with pytest.raises(OSError):
        with lock_generation_shared(link):
            pass


def test_lock_generation_shared_holds_off_the_pruner(graph):
    link, make = graph
    generation = make("20260101T000000")
    swap_graph_link(link, generation)

    with lock_generation_shared(link):
        with lock_exclusive(generation) as acquired:
            assert acquired is False


def test_lock_generation_shared_re_resolves_when_the_symlink_moved(graph, monkeypatch):
    link, make = graph
    first = make("20260101T000000")
    second = make("20260108T000000")
    swap_graph_link(link, first)

    acquires = []
    real_acquire = lock_utils._acquire

    def moving_acquire(path, mode, timeout):
        lock_id = real_acquire(path, mode, timeout)
        acquires.append(path)
        if len(acquires) == 1:
            swap_graph_link(link, second)

        return lock_id

    monkeypatch.setattr(lock_utils, "_acquire", moving_acquire)

    with lock_generation_shared(link) as resolved:
        assert resolved == second

    assert len(acquires) == 2


def test_lock_generation_shared_releases_the_generation_it_gave_up_on(graph, monkeypatch):
    link, make = graph
    first = make("20260101T000000")
    second = make("20260108T000000")
    swap_graph_link(link, first)

    real_acquire = lock_utils._acquire
    moved = []

    def moving_acquire(path, mode, timeout):
        lock_id = real_acquire(path, mode, timeout)
        if not moved:
            moved.append(True)
            swap_graph_link(link, second)

        return lock_id

    monkeypatch.setattr(lock_utils, "_acquire", moving_acquire)

    with lock_generation_shared(link):
        pass

    monkeypatch.undo()
    with lock_exclusive(first) as acquired:
        assert acquired is True
