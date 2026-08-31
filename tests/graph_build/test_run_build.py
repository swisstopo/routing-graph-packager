import json

import pytest

from routing_packager_app.config import SETTINGS
from routing_packager_app.constants import BuildOutcome, LockMode
from routing_packager_app.graph_build import __main__ as graph_build_main
from routing_packager_app.graph_build.builder import BuildError
from routing_packager_app.graph_build.status import BUILD_STATUS, EXTERNAL_SCHEDULE, BuildStatus
from routing_packager_app.utils.lock_utils import _release, _try_acquire, lock_path


@pytest.fixture
def build_lock_held():
    lock_id = _try_acquire(lock_path(SETTINGS.get_provider_dir("osm")), LockMode.EXCLUSIVE)
    try:
        yield lock_id
    finally:
        _release(lock_id)


@pytest.fixture
def build_env(tmp_path, monkeypatch):
    monkeypatch.setattr(SETTINGS, "TMP_DATA_DIR", tmp_path)
    pbf = tmp_path.joinpath("planet.osm.pbf")
    pbf.write_bytes(b"")
    monkeypatch.setattr(SETTINGS, "PBF_LOCAL_PATH", pbf)
    monkeypatch.setattr(graph_build_main, "update_pbf", lambda _: None)

    enqueued = []

    async def fake_enqueue():
        enqueued.append(True)

    monkeypatch.setattr(graph_build_main, "_enqueue_package_updates", fake_enqueue)

    status_path = tmp_path.joinpath("build_status.json")
    monkeypatch.setattr(BUILD_STATUS, "path", status_path)
    monkeypatch.setattr(BUILD_STATUS, "_data", BuildStatus(status_path)._data)
    monkeypatch.setattr(BUILD_STATUS, "_last_heartbeat", 0.0)

    return tmp_path, enqueued


def fake_build_factory(name):
    def fake_build(generations_dir, _pbf):
        generation = generations_dir.joinpath(name)
        generation.mkdir(parents=True)
        generation.joinpath("tile.gph").write_bytes(b"tile")

        return generation

    return fake_build


def test_run_build_swaps_link_and_enqueues(build_env, monkeypatch):
    _, enqueued = build_env
    monkeypatch.setattr(graph_build_main, "build_graph", fake_build_factory("20260201T000000"))

    graph_build_main.run_build("osm")

    link = SETTINGS.get_graph_link()
    assert link.is_symlink()
    assert link.resolve().name == "20260201T000000"
    assert link.joinpath("build_meta.json").is_file()
    assert enqueued == [True]


def test_second_build_prunes_the_previous_generation(build_env, monkeypatch):
    _, _ = build_env
    monkeypatch.setattr(graph_build_main, "build_graph", fake_build_factory("20260201T000000"))
    graph_build_main.run_build("osm")

    monkeypatch.setattr(graph_build_main, "build_graph", fake_build_factory("20260208T000000"))
    graph_build_main.run_build("osm")

    generations = sorted(p.name for p in SETTINGS.get_generations_dir().iterdir())
    assert generations == ["20260201T000000", "20260208T000000"]

    monkeypatch.setattr(graph_build_main, "build_graph", fake_build_factory("20260215T000000"))
    graph_build_main.run_build("osm")

    generations = sorted(p.name for p in SETTINGS.get_generations_dir().iterdir())
    assert generations == ["20260208T000000", "20260215T000000"]
    assert SETTINGS.get_graph_link().resolve().name == "20260215T000000"


def test_failed_build_keeps_the_current_graph(build_env, monkeypatch):
    _, enqueued = build_env
    monkeypatch.setattr(graph_build_main, "build_graph", fake_build_factory("20260201T000000"))
    graph_build_main.run_build("osm")
    enqueued.clear()

    def failing_build(*_args):
        raise BuildError("valhalla_build_tiles blew up")

    monkeypatch.setattr(graph_build_main, "build_graph", failing_build)
    with pytest.raises(BuildError):
        graph_build_main.run_build("osm")

    assert SETTINGS.get_graph_link().resolve().name == "20260201T000000"
    assert enqueued == []


def test_run_build_reports_going_idle(build_env, monkeypatch):
    monkeypatch.setattr(graph_build_main, "build_graph", fake_build_factory("20260201T000000"))
    graph_build_main.run_build("osm")
    report = json.loads(BUILD_STATUS.path.read_text())

    assert report["state"] == "idle"
    assert report["stage"] is None


def test_run_build_reports_the_stage_it_reached(build_env, monkeypatch):
    def failing_build(*_args):
        raise BuildError("valhalla_build_tiles blew up")

    monkeypatch.setattr(graph_build_main, "build_graph", failing_build)
    with pytest.raises(BuildError):
        graph_build_main.run_build("osm")
    report = json.loads(BUILD_STATUS.path.read_text())

    assert report["state"] == "building"
    assert report["stage"] == "pruning"


def test_run_build_skips_when_another_build_holds_the_lock(build_env, build_lock_held, monkeypatch):
    _, enqueued = build_env
    monkeypatch.setattr(graph_build_main, "build_graph", fake_build_factory("20260201T000000"))

    graph_build_main.run_build("osm")

    assert not SETTINGS.get_graph_link().is_symlink()
    assert enqueued == []


def test_run_build_reports_a_skipped_run(build_env, build_lock_held, monkeypatch):
    monkeypatch.setattr(graph_build_main, "build_graph", fake_build_factory("20260201T000000"))

    assert graph_build_main.run_build("osm") is BuildOutcome.SKIPPED


def test_once_builds_a_single_graph(build_env, monkeypatch):
    _, enqueued = build_env
    monkeypatch.setattr(graph_build_main, "build_graph", fake_build_factory("20260201T000000"))

    assert graph_build_main.main(["--once"]) == graph_build_main.EXIT_OK
    assert SETTINGS.get_graph_link().resolve().name == "20260201T000000"
    assert enqueued == [True]


def test_once_ignores_the_cron_expression(build_env, monkeypatch):
    monkeypatch.setattr(SETTINGS, "GRAPH_BUILD_CRON", "not a cron expression")
    monkeypatch.setattr(graph_build_main, "build_graph", fake_build_factory("20260201T000000"))

    assert graph_build_main.main(["--once"]) == graph_build_main.EXIT_OK


def test_once_never_guesses_the_next_build(build_env, monkeypatch):
    monkeypatch.setattr(SETTINGS, "GRAPH_BUILD_CRON", "0 3 * * 0")
    monkeypatch.setattr(graph_build_main, "build_graph", fake_build_factory("20260201T000000"))

    graph_build_main.main(["--once"])

    assert json.loads(BUILD_STATUS.path.read_text())["next_build_at"] == EXTERNAL_SCHEDULE


def test_once_fails_with_an_exit_code(build_env, monkeypatch):
    _, enqueued = build_env

    def failing_build(*_args):
        raise BuildError("valhalla_build_tiles blew up")

    monkeypatch.setattr(graph_build_main, "build_graph", failing_build)

    assert graph_build_main.main(["--once"]) == graph_build_main.EXIT_FAILED
    report = json.loads(BUILD_STATUS.path.read_text())
    assert report["state"] == "failed"
    assert report["last_error"] == "valhalla_build_tiles blew up"
    assert enqueued == []


def test_once_reports_a_concurrent_build(build_env, build_lock_held, monkeypatch):
    monkeypatch.setattr(graph_build_main, "build_graph", fake_build_factory("20260201T000000"))

    assert graph_build_main.main(["--once"]) == graph_build_main.EXIT_LOCKED
