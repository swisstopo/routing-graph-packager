import os
import subprocess
import sys
import threading

import pytest
from sqlalchemy import text

from routing_packager_app.db import lock_engine
from routing_packager_app.graph_build.builder import swap_graph_link
from routing_packager_app.utils import lock_utils
from routing_packager_app.utils.lock_utils import (
    build_lock_name,
    generation_lock_name,
    lock_exclusive,
    lock_generation_shared,
    lock_key,
)

KEY_IN_SUBPROCESS = """
import sys
sys.path.insert(0, %r)
from routing_packager_app.utils.lock_utils import lock_key
print(lock_key("rgp:build:osm"))
"""


@pytest.fixture
def name(tmp_path):
    return build_lock_name(tmp_path.name)


@pytest.fixture
def graph(tmp_path):
    generations = tmp_path.joinpath("generations")
    generations.mkdir()
    link = tmp_path.joinpath("graph")

    def make(generation_name):
        generation = generations.joinpath(generation_name)
        generation.mkdir()
        generation.joinpath("tile.gph").write_bytes(b"tile")

        return generation

    return link, make


def hold(name, shared=False):
    function = "pg_advisory_lock_shared" if shared else "pg_advisory_lock"
    holder = lock_engine.connect()
    holder.execute(text(f"SELECT {function}(:key)"), {"key": lock_key(name)})
    holder.commit()

    return holder


def test_exclusive_is_granted_when_nobody_holds_it(name):
    with lock_exclusive(name) as acquired:
        assert acquired is True


def test_exclusive_excludes_another_exclusive(name):
    holder = hold(name)
    try:
        with lock_exclusive(name) as acquired:
            assert acquired is False
    finally:
        holder.close()


def test_exclusive_is_released_on_exit(name):
    with lock_exclusive(name) as acquired:
        assert acquired is True

    with lock_exclusive(name) as acquired:
        assert acquired is True


def test_exclusive_is_released_when_the_body_raises(name):
    with pytest.raises(ValueError):
        with lock_exclusive(name):
            raise ValueError("boom")

    with lock_exclusive(name) as acquired:
        assert acquired is True


def test_shared_does_not_exclude_shared(name):
    first = hold(name, shared=True)
    second = hold(name, shared=True)
    try:
        assert first is not second
    finally:
        first.close()
        second.close()


def test_exclusive_excludes_a_shared_holder(name):
    holder = hold(name, shared=True)
    try:
        with lock_exclusive(name) as acquired:
            assert acquired is False
    finally:
        holder.close()

    with lock_exclusive(name) as acquired:
        assert acquired is True


def test_exclusive_waits_out_a_shared_holder(name, monkeypatch):
    monkeypatch.setattr(lock_utils, "LOCK_POLL_INTERVAL", 0.05)
    holder = hold(name, shared=True)
    threading.Timer(0.3, holder.close).start()

    with lock_exclusive(name, timeout=30) as acquired:
        assert acquired is True


def test_exclusive_gives_up_after_the_timeout(name, monkeypatch):
    monkeypatch.setattr(lock_utils, "LOCK_POLL_INTERVAL", 0.05)
    holder = hold(name, shared=True)
    try:
        with lock_exclusive(name, timeout=0.2) as acquired:
            assert acquired is False
    finally:
        holder.close()


def test_closing_a_connection_releases_its_lock(name):
    holder = hold(name)
    holder.close()

    with lock_exclusive(name) as acquired:
        assert acquired is True


def test_a_holder_does_not_sit_in_a_transaction(name):
    with lock_exclusive(name) as acquired:
        assert acquired is True
        with lock_engine.connect() as observer:
            states = observer.execute(
                text(
                    "SELECT DISTINCT a.state FROM pg_stat_activity a "
                    "JOIN pg_locks l ON l.pid = a.pid "
                    "WHERE l.locktype = 'advisory' AND a.pid <> pg_backend_pid()"
                )
            ).scalars()

            assert list(states) == ["idle"]


def test_the_key_is_stable_across_processes():
    env = dict(os.environ, PYTHONHASHSEED="1")
    out = subprocess.run(
        [sys.executable, "-c", KEY_IN_SUBPROCESS % os.getcwd()],
        capture_output=True,
        text=True,
        env=env,
    )

    assert int(out.stdout.strip()) == lock_key("rgp:build:osm")


def test_the_key_fits_a_postgres_bigint():
    key = lock_key("rgp:generation:osm:20260201T000000")

    assert -(2**63) <= key < 2**63


def test_generation_keys_are_namespaced_per_provider():
    assert lock_key(generation_lock_name("osm", "20260201T000000")) != lock_key(
        generation_lock_name("here", "20260201T000000")
    )


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
        name = generation_lock_name(link.parent.name, generation.name)
        with lock_exclusive(name) as acquired:
            assert acquired is False


def test_lock_generation_shared_re_resolves_when_the_symlink_moved(graph, monkeypatch):
    link, make = graph
    first = make("20260101T000000")
    second = make("20260108T000000")
    swap_graph_link(link, first)

    acquires = []
    real_acquire = lock_utils._acquire

    def moving_acquire(conn, function, key, timeout):
        acquired = real_acquire(conn, function, key, timeout)
        acquires.append(key)
        if len(acquires) == 1:
            swap_graph_link(link, second)

        return acquired

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

    def moving_acquire(conn, function, key, timeout):
        acquired = real_acquire(conn, function, key, timeout)
        if not moved:
            moved.append(True)
            swap_graph_link(link, second)

        return acquired

    monkeypatch.setattr(lock_utils, "_acquire", moving_acquire)

    with lock_generation_shared(link):
        pass

    monkeypatch.undo()
    with lock_exclusive(generation_lock_name(link.parent.name, first.name)) as acquired:
        assert acquired is True
