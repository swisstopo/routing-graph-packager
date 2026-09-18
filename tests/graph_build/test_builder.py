import json
import logging
import shutil
import sys
import threading

import pytest

from routing_packager_app.constants import LockMode
from routing_packager_app.graph_build import builder
from routing_packager_app.graph_build.builder import (
    BuildError,
    _log_tag,
    _run,
    build_graph,
    prune_generations,
    swap_graph_link,
    update_pbf,
)
from routing_packager_app.utils.lock_utils import (
    _release,
    _try_acquire,
    lock_generation_shared,
    lock_path,
)


def make_generation(generations, name):
    generation = generations.joinpath(name)
    generation.mkdir()
    generation.joinpath("tile.gph").write_bytes(b"tile")

    return generation


def hold_shared_lock(generation):
    return _try_acquire(lock_path(generation), LockMode.SHARED)


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

    pruned = prune_generations(generations, link)

    assert pruned == [old]
    assert not old.exists()
    assert current.is_dir()


def test_prune_retains_extra_generations(graph_dirs, monkeypatch):
    monkeypatch.setattr("routing_packager_app.graph_build.builder.KEEP_GENERATIONS", 2)
    generations, link = graph_dirs
    oldest = make_generation(generations, "20260101T000000")
    previous = make_generation(generations, "20260108T000000")
    current = make_generation(generations, "20260115T000000")
    swap_graph_link(link, current)

    pruned = prune_generations(generations, link)

    assert pruned == [oldest]
    assert previous.is_dir()
    assert current.is_dir()


def test_prune_aborts_the_build_when_a_generation_stays_held(graph_dirs):
    generations, link = graph_dirs
    old = make_generation(generations, "20260101T000000")
    current = make_generation(generations, "20260108T000000")
    swap_graph_link(link, current)

    lock_id = hold_shared_lock(old)
    try:
        with pytest.raises(BuildError, match="still held by a packaging job"):
            prune_generations(generations, link, timeout=0.2)
    finally:
        _release(lock_id)

    assert old.is_dir()
    assert current.is_dir()

    assert prune_generations(generations, link) == [old]
    assert not old.exists()


def test_prune_waits_for_a_reader_to_finish(graph_dirs, monkeypatch):
    monkeypatch.setattr("routing_packager_app.utils.lock_utils.LOCK_POLL_INTERVAL", 0.05)
    generations, link = graph_dirs
    old = make_generation(generations, "20260101T000000")
    current = make_generation(generations, "20260108T000000")
    swap_graph_link(link, current)

    lock_id = hold_shared_lock(old)
    threading.Timer(0.3, _release, [lock_id]).start()
    pruned = prune_generations(generations, link, timeout=30)

    assert pruned == [old]
    assert not old.exists()


def test_update_pbf_retries_until_current(tmp_path, monkeypatch):
    pbf = tmp_path.joinpath("planet.osm.pbf")
    pbf.write_bytes(b"")
    codes = iter([1, 1, 0])
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return next(codes)

    monkeypatch.setattr("routing_packager_app.graph_build.builder._run", fake_run)
    update_pbf(pbf)

    assert len(calls) == 3


def test_update_pbf_raises_on_server_error(tmp_path, monkeypatch):
    pbf = tmp_path.joinpath("planet.osm.pbf")
    pbf.write_bytes(b"")

    monkeypatch.setattr("routing_packager_app.graph_build.builder._run", lambda cmd, **kwargs: 3)
    with pytest.raises(BuildError, match="exit code 3"):
        update_pbf(pbf)


def test_update_pbf_raises_when_never_current(tmp_path, monkeypatch):
    pbf = tmp_path.joinpath("planet.osm.pbf")
    pbf.write_bytes(b"")

    monkeypatch.setattr("routing_packager_app.config.SETTINGS.PBF_MAX_UPDATE_PASSES", 2)
    monkeypatch.setattr("routing_packager_app.graph_build.builder._run", lambda cmd, **kwargs: 1)
    with pytest.raises(BuildError, match="still behind"):
        update_pbf(pbf)


def test_run_forwards_stdout_and_stderr_to_the_builder_log(caplog):
    caplog.set_level(logging.INFO, logger="builder")
    _run([
        sys.executable,
        "-c",
        "import sys; print('to stdout'); print('to stderr', file=sys.stderr)",
    ])

    messages = [record.message for record in caplog.records]
    tag = _log_tag([sys.executable])
    assert f"{tag} to stdout" in messages
    assert f"{tag} to stderr" in messages


def test_run_logs_stderr_while_stdout_is_redirected(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="builder")
    out = tmp_path.joinpath("out.json")
    with open(out, "w") as fh:
        _run(
            [
                sys.executable,
                "-c",
                "import sys; print('payload'); print('noise', file=sys.stderr)",
            ],
            stdout=fh,
        )

    assert out.read_text() == "payload\n"
    tag = _log_tag([sys.executable])
    assert f"{tag} noise" in [record.message for record in caplog.records]


def test_run_returns_the_exit_code_when_not_checking():
    assert _run([sys.executable, "-c", "import sys; sys.exit(3)"], check=False) == 3


def test_run_raises_on_a_failing_command():
    with pytest.raises(BuildError, match="exit code 3"):
        _run([sys.executable, "-c", "import sys; sys.exit(3)"])


def test_build_graph_passes_top_level_logging_flags(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, stdout=None, check=True):
        calls.append(cmd)
        if stdout:
            stdout.write("{}")
        return 0

    monkeypatch.setattr("routing_packager_app.config.SETTINGS.USE_ELEVATION", False)
    monkeypatch.setattr(builder, "_run", fake_run)
    build_graph(tmp_path.joinpath("generations"), tmp_path.joinpath("planet.osm.pbf"))

    config_cmd = calls[0]
    assert "--mjolnir-logging-type" not in config_cmd
    assert config_cmd[config_cmd.index("--logging-type") + 1] == "std_out"
    assert config_cmd[config_cmd.index("--logging-color") + 1] == "false"


@pytest.mark.skipif(shutil.which("valhalla_build_config") is None, reason="Valhalla is not installed")
def test_build_graph_writes_a_valid_valhalla_config(tmp_path, monkeypatch):
    real_run = builder._run

    def fake_run(cmd, stdout=None, check=True):
        if cmd[0].endswith("valhalla_build_config"):
            return real_run(cmd, stdout=stdout, check=check)
        return 0

    monkeypatch.setattr("routing_packager_app.config.SETTINGS.USE_ELEVATION", False)
    monkeypatch.setattr(builder, "_run", fake_run)
    generation = build_graph(tmp_path.joinpath("generations"), tmp_path.joinpath("planet.osm.pbf"))

    config = json.loads(generation.joinpath("valhalla.json").read_text())
    assert config["logging"]["type"] == "std_out"
    assert config["logging"]["color"] is False
    assert config["mjolnir"]["tile_dir"] == str(generation)


def test_valhalla_binaries_share_one_log_tag():
    assert _log_tag(["/usr/local/bin/valhalla_build_tiles"]) == "[VALHALLA]"
    assert _log_tag(["/usr/local/bin/valhalla_build_config"]) == "[VALHALLA]"
    assert _log_tag(["/usr/local/bin/valhalla_build_elevation"]) == "[VALHALLA]"


def test_other_tools_are_not_tagged_as_valhalla():
    assert _log_tag(["wget"]) == "[WGET]"
    assert _log_tag(["/app/app_venv/bin/pyosmium-up-to-date"]) == "[PYOSMIUM-UP-TO-DATE]"


def test_run_tags_every_line_of_valhalla_output(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="builder")
    fake = tmp_path.joinpath("valhalla_build_tiles")
    fake.write_text(
        "#!/bin/sh\necho 'Parsing ways...'\necho 'Finished' >&2\n",
        encoding="utf8",
    )
    fake.chmod(0o755)
    _run([str(fake)])

    messages = [record.message for record in caplog.records]
    assert "[VALHALLA] Parsing ways..." in messages
    assert "[VALHALLA] Finished" in messages


def test_terminate_current_without_a_child_is_a_no_op():
    builder.terminate_current()


def test_terminate_current_stops_a_running_build(tmp_path):
    fake = tmp_path.joinpath("valhalla_build_tiles")
    fake.write_text("#!/bin/sh\necho 'started'\nexec sleep 60\n", encoding="utf8")
    fake.chmod(0o755)

    stopper = threading.Timer(1.0, builder.terminate_current)
    stopper.start()
    try:
        with pytest.raises(BuildError):
            _run([str(fake)])
    finally:
        stopper.cancel()

    assert builder._current is None
