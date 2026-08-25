import subprocess
import sys
import threading

import pytest

from routing_packager_app.graph_build.builder import (
    BuildError,
    prune_generations,
    swap_graph_link,
    update_pbf,
)
from routing_packager_app.utils.file_utils import (
    LOCK_NAME,
    create_lock_file,
    lock_generation_shared,
)

HOLD_SHARED_LOCK = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDONLY)
fcntl.flock(fd, fcntl.LOCK_SH)
print("locked", flush=True)
time.sleep(60)
"""


@pytest.fixture
def graph_dirs(tmp_path):
    generations = tmp_path.joinpath("generations")
    generations.mkdir()
    link = tmp_path.joinpath("graph")

    return generations, link


def make_generation(generations, name):
    generation = generations.joinpath(name)
    generation.mkdir()
    create_lock_file(generation)
    generation.joinpath("tile.gph").write_bytes(b"tile")

    return generation


def hold_shared_lock(lock_path):
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLD_SHARED_LOCK, str(lock_path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout.readline().strip() == "locked"

    return holder


def test_swap_graph_link_repoints(graph_dirs):
    generations, link = graph_dirs
    first = make_generation(generations, "20260101T000000")
    second = make_generation(generations, "20260108T000000")

    swap_graph_link(link, first)
    assert link.resolve() == first

    swap_graph_link(link, second)
    assert link.resolve() == second
    assert not link.with_name("graph.tmp").exists()


def test_lock_generation_shared_resolves_symlink(graph_dirs):
    generations, link = graph_dirs
    generation = make_generation(generations, "20260101T000000")
    swap_graph_link(link, generation)

    with lock_generation_shared(link) as resolved:
        assert resolved == generation


def test_lock_generation_shared_without_graph(graph_dirs):
    _, link = graph_dirs
    with pytest.raises(OSError):
        with lock_generation_shared(link):
            pass


def test_prune_keeps_current_generation(graph_dirs):
    generations, link = graph_dirs
    old = make_generation(generations, "20260101T000000")
    current = make_generation(generations, "20260108T000000")
    swap_graph_link(link, current)

    pruned = prune_generations(generations, link, keep=1)

    assert pruned == [old]
    assert not old.exists()
    assert current.is_dir()


def test_prune_retains_extra_generations(graph_dirs):
    generations, link = graph_dirs
    oldest = make_generation(generations, "20260101T000000")
    previous = make_generation(generations, "20260108T000000")
    current = make_generation(generations, "20260115T000000")
    swap_graph_link(link, current)

    pruned = prune_generations(generations, link, keep=2)

    assert pruned == [oldest]
    assert previous.is_dir()
    assert current.is_dir()


def test_prune_aborts_the_build_when_a_generation_stays_held(graph_dirs):
    generations, link = graph_dirs
    old = make_generation(generations, "20260101T000000")
    current = make_generation(generations, "20260108T000000")
    swap_graph_link(link, current)

    holder = hold_shared_lock(old.joinpath(LOCK_NAME))
    try:
        with pytest.raises(BuildError, match="still held by a packaging job"):
            prune_generations(generations, link, keep=1, timeout=0.2)
    finally:
        holder.kill()
        holder.wait()

    assert old.is_dir()
    assert current.is_dir()

    assert prune_generations(generations, link, keep=1) == [old]
    assert not old.exists()


def test_prune_waits_for_a_reader_to_finish(graph_dirs, monkeypatch):
    monkeypatch.setattr("routing_packager_app.utils.file_utils.LOCK_POLL_INTERVAL", 0.05)
    generations, link = graph_dirs
    old = make_generation(generations, "20260101T000000")
    current = make_generation(generations, "20260108T000000")
    swap_graph_link(link, current)

    holder = hold_shared_lock(old.joinpath(LOCK_NAME))
    threading.Timer(0.3, holder.kill).start()
    try:
        pruned = prune_generations(generations, link, keep=1, timeout=30)
    finally:
        holder.wait()

    assert pruned == [old]
    assert not old.exists()


def test_update_pbf_retries_until_current(tmp_path, monkeypatch):
    pbf = tmp_path.joinpath("planet.osm.pbf")
    pbf.write_bytes(b"")
    codes = iter([1, 1, 0])
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, next(codes))

    monkeypatch.setattr("routing_packager_app.graph_build.builder.subprocess.run", fake_run)
    update_pbf(pbf)

    assert len(calls) == 3


def test_update_pbf_raises_on_server_error(tmp_path, monkeypatch):
    pbf = tmp_path.joinpath("planet.osm.pbf")
    pbf.write_bytes(b"")

    monkeypatch.setattr(
        "routing_packager_app.graph_build.builder.subprocess.run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 3),
    )
    with pytest.raises(BuildError, match="exit code 3"):
        update_pbf(pbf)


def test_update_pbf_raises_when_never_current(tmp_path, monkeypatch):
    pbf = tmp_path.joinpath("planet.osm.pbf")
    pbf.write_bytes(b"")

    monkeypatch.setattr("routing_packager_app.config.SETTINGS.PBF_MAX_UPDATE_PASSES", 2)
    monkeypatch.setattr(
        "routing_packager_app.graph_build.builder.subprocess.run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1),
    )
    with pytest.raises(BuildError, match="still behind"):
        update_pbf(pbf)
