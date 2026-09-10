import json
from datetime import datetime, timezone

import pytest

from routing_packager_app.config import SETTINGS
from routing_packager_app.constants import BuildStage, BuildState
from routing_packager_app.graph_build.status import BuildStatus


@pytest.fixture
def status(tmp_path):
    return BuildStatus(tmp_path.joinpath("build_status.json"))


def test_starts_out_unwritten(status):
    assert not status.path.exists()


def test_records_a_stage(status):
    status.stage(BuildStage.UPDATING_PBF)
    report = json.loads(status.path.read_text())

    assert report["state"] == BuildState.BUILDING.value
    assert report["stage"] == BuildStage.UPDATING_PBF.value
    assert report["started_at"] is not None
    assert report["updated_at"] is not None


def test_keeps_started_at_across_stages(status):
    status.stage(BuildStage.PRUNING)
    started_at = json.loads(status.path.read_text())["started_at"]
    status.stage(BuildStage.BUILDING_TILES)
    report = json.loads(status.path.read_text())

    assert report["started_at"] == started_at
    assert report["stage"] == BuildStage.BUILDING_TILES.value


def test_leaves_no_temporary_file_behind(status):
    status.stage(BuildStage.PRUNING)

    assert [p.name for p in status.path.parent.iterdir()] == ["build_status.json"]


def test_records_the_generation(status):
    status.stage(BuildStage.BUILDING_TILES)
    status.generation("20260825T113047")

    assert json.loads(status.path.read_text())["generation"] == "20260825T113047"


def test_heartbeat_is_throttled(status):
    status.stage(BuildStage.BUILDING_TILES)
    updated_at = json.loads(status.path.read_text())["updated_at"]
    for _ in range(100):
        status.heartbeat()

    assert json.loads(status.path.read_text())["updated_at"] == updated_at


def test_heartbeat_writes_once_the_interval_passed(status, monkeypatch):
    status.stage(BuildStage.BUILDING_TILES)
    updated_at = json.loads(status.path.read_text())["updated_at"]
    monkeypatch.setattr("routing_packager_app.graph_build.status.HEARTBEAT_INTERVAL", 0.0)
    status.heartbeat()

    assert json.loads(status.path.read_text())["updated_at"] != updated_at


def test_going_idle_clears_the_stage(status):
    status.stage(BuildStage.SWAPPING)
    when = datetime(2026, 9, 1, 3, tzinfo=timezone.utc)
    status.idle(when)
    report = json.loads(status.path.read_text())

    assert report["state"] == BuildState.IDLE.value
    assert report["stage"] is None
    assert report["next_build_at"] == when.isoformat()


def test_failing_keeps_the_stage_it_died_on(status):
    status.stage(BuildStage.UPDATING_PBF)
    status.failed("pyosmium-up-to-date failed with exit code 3")
    report = json.loads(status.path.read_text())

    assert report["state"] == BuildState.FAILED.value
    assert report["stage"] == BuildStage.UPDATING_PBF.value
    assert report["last_error"] == "pyosmium-up-to-date failed with exit code 3"


def test_a_new_build_clears_the_previous_error(status):
    status.stage(BuildStage.UPDATING_PBF)
    status.failed("boom")
    status.stage(BuildStage.PRUNING)
    report = json.loads(status.path.read_text())

    assert report["state"] == BuildState.BUILDING.value
    assert report["last_error"] is None


def test_a_write_failure_never_breaks_a_build(tmp_path):
    status = BuildStatus(tmp_path.joinpath("missing").joinpath("x").joinpath("build_status.json"))
    status.path.parent.parent.write_text("not a directory")
    status.stage(BuildStage.PRUNING)

    assert not status.path.exists()


def test_an_unbound_status_refuses_to_write():
    """A builder that never bound a provider has no file it could sensibly write to."""
    with pytest.raises(RuntimeError) as e:
        BuildStatus().stage(BuildStage.PRUNING)

    assert "bind" in str(e.value)


def test_binding_points_at_that_providers_file():
    status = BuildStatus()
    status.bind("tomtom")

    assert status.path == SETTINGS.get_build_status_path("tomtom")
    assert status.provider == "tomtom"


def test_binding_starts_from_a_clean_slate(tmp_path):
    status = BuildStatus(tmp_path.joinpath("build_status.json"))
    status.failed("boom")

    status.bind("osm")

    assert status._data["state"] == BuildState.UNKNOWN.value
    assert status._data["last_error"] is None


def test_two_providers_write_to_separate_files():
    osm, tomtom = BuildStatus(), BuildStatus()
    osm.bind("osm")
    tomtom.bind("tomtom")
    osm.failed("osm broke")
    tomtom.idle()

    assert json.loads(osm.path.read_text())["state"] == BuildState.FAILED.value
    assert json.loads(tomtom.path.read_text())["state"] == BuildState.IDLE.value
