"""
Builds Valhalla graphs into isolated, self-contained generations.

A **generation** is one graph: the complete tile set produced by a single build run, living in
its own timestamped directory. Generations are never modified once built. A build creates a
new one, and on success the ``graph`` symlink is moved to point at it, which is what makes it
the one packaging jobs read from. Older generations stay on disk untouched until a later build
prunes them. By default, no more than two generations exist on disk: one serving as the final graph,
one being currently built.
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, TextIO

from ..config import SETTINGS
from ..constants import BuildStage
from ..logger import BUILD_LOGGER
from ..utils.file_utils import LOCK_NAME, create_lock_file, lock_exclusive
from .status import BUILD_STATUS

GENERATION_FORMAT = "%Y%m%dT%H%M%S"

_current: subprocess.Popen | None = None


class BuildError(Exception):
    """Raised when a graph build step fails and the symlink must not be swapped."""


def _binary(name: str) -> str:
    candidate = Path(sys.executable).parent.joinpath(name)
    if candidate.is_file():
        return str(candidate)

    return shutil.which(name) or name


def _log_tag(cmd: List[str]) -> str:
    """
    Builds the marker every line of a subprocess' output is prefixed with.

    Valhalla's binaries all share one ``[VALHALLA]`` tag so that a whole tile build can be
    grepped out of ``builder.log`` in one go, separate from the build loop's own messages. The
    other tools we shell out to get their own name rather than being mislabelled.

    :param cmd: the command about to run.
    """
    name = Path(cmd[0]).name
    if name.startswith("valhalla_"):
        return "[VALHALLA]"

    return f"[{name.upper()}]"


def terminate_current() -> None:
    """
    Stops the subprocess a build is currently waiting on, if there is one.

    Called from the signal handler: a build spends nearly all of its wall clock inside one of
    Valhalla's binaries, so a shutdown that does not reach the child only ends in the kernel
    SIGKILLing it once the grace period runs out.
    """
    process = _current
    if process is not None and process.poll() is None:
        BUILD_LOGGER.info(f"Terminating {' '.join(process.args)}.")
        process.terminate()


def _run(cmd: List[str], stdout: TextIO | None = None, check: bool = True) -> int:
    global _current

    BUILD_LOGGER.info(f"Running {' '.join(cmd)}")
    tag = _log_tag(cmd)
    with subprocess.Popen(
        cmd,
        stdout=stdout or subprocess.PIPE,
        stderr=subprocess.PIPE if stdout else subprocess.STDOUT,
        text=True,
        bufsize=1,
    ) as process:
        _current = process
        try:
            for line in process.stderr if stdout else process.stdout:
                BUILD_LOGGER.info(f"{tag} {line.rstrip()}")
                BUILD_STATUS.heartbeat()
        finally:
            _current = None

    if check and process.returncode != 0:
        raise BuildError(f"'{cmd[0]}' failed with exit code {process.returncode}")

    return process.returncode


def _valhalla_version() -> str:
    try:
        out = subprocess.run(
            [_binary("valhalla_build_tiles"), "--version"], capture_output=True, text=True, timeout=30
        )
        return (out.stdout or out.stderr).strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _remove_generation(generation: Path) -> Path:
    shutil.rmtree(generation, ignore_errors=True)
    BUILD_LOGGER.info(f"Pruned generation {generation.name}.")

    return generation


def prune_generations(generations_dir: Path, link: Path, keep: int, timeout: float = 0.0) -> List[Path]:
    """
    Deletes graph generations that are neither current nor still held by a packaging job.

    This is the only place tiles are ever deleted. A generation is only removed once an
    exclusive lock on its lock file is granted, which guards it against workers holding a
    shared lock for the duration of a zip.

    Pruning has to finish before the build starts, because the build writes one more tile set
    to disk. Skipping a locked generation and building anyway would leave `keep + 2` tile sets
    where there is only room for `keep + 1`, so a held generation is waited on for up to
    `timeout` seconds and the build is aborted if it is still held after that. Aborting keeps
    the current graph in place and the next scheduled run tries again.

    :param generations_dir: the directory holding every built generation.
    :param link: the graph symlink, whose target is never pruned.
    :param keep: how many generations to retain, the current one included.
    :param timeout: how many seconds to wait for a generation held by a packaging job.

    :returns: the pruned generation directories.
    """

    # if the passed directory does not exist, there is nothing to do
    BUILD_STATUS.stage(BuildStage.PRUNING)
    if not generations_dir.is_dir():
        return []

    # try to follow the symlink to the current directory inside
    # the generations dir
    current = link.resolve() if link.is_symlink() else None

    # list all the graph builds that are not serving as the current one used
    # in packaging
    # names are timestamps, so reverse order means newest to oldest
    others = sorted((p for p in generations_dir.iterdir() if p.is_dir() and p != current), reverse=True)
    # retain the <keep> newest ones
    retained = set(others[: max(keep - 1, 0)])

    pruned: List[Path] = []
    for generation in others:
        if generation in retained:
            continue

        lock_path = generation.joinpath(LOCK_NAME)
        # try to prune
        with lock_exclusive(lock_path) as acquired:
            if acquired:
                pruned.append(_remove_generation(generation))
                continue

        # if we get here acquiring the lock failed and we'll
        # patiently try again for timeout seconds
        BUILD_LOGGER.info(
            f"Generation {generation.name} is held by a packaging job, waiting up to "
            f"{timeout:.0f}s before starting the build."
        )
        with lock_exclusive(lock_path, timeout) as acquired:
            # the default timeout of one hour will rarely be
            # exceeded... _maybe_ if the registered packages stack
            # up over time
            if not acquired:
                raise BuildError(
                    f"Generation {generation.name} is still held by a packaging job after "
                    f"{timeout:.0f}s. Aborting so that no more than {keep + 1} tile sets end up "
                    "on disk."
                )
            pruned.append(_remove_generation(generation))

    return pruned


def download_pbf(pbf: Path) -> None:
    """
    Downloads the configured PBF to its local path.

    :param pbf: where the PBF should end up.
    """
    BUILD_STATUS.stage(BuildStage.DOWNLOADING_PBF)
    pbf.parent.mkdir(parents=True, exist_ok=True)
    BUILD_LOGGER.info(f"Downloading {SETTINGS.PBF_URL} to {pbf}")
    _run(["wget", "-nv", SETTINGS.PBF_URL, "-O", str(pbf)])


def update_pbf(pbf: Path) -> None:
    """
    Brings the local PBF up to date with its replication server.

    ``pyosmium-up-to-date`` exits 0 when the file is fully current, 1 when it applied diffs but
    more remain behind the ``--size`` cap, and 2/3 on error. Exit code 1 is therefore a success
    that needs another pass, which is what the loop below is for.

    :param pbf: the PBF to update in place.

    Note that pyosmium does not expose this functionality conveniently via the library, so we
    have to use subprocess.
    """
    BUILD_STATUS.stage(BuildStage.UPDATING_PBF)
    cmd = [
        _binary("pyosmium-up-to-date"),
        "-v",
        "--size",
        str(SETTINGS.PBF_UPDATE_SIZE_MB),
        "--socket-timeout",
        "60",
    ]
    if SETTINGS.PBF_FORCE_UPDATE:
        cmd.append("--force-update-of-old-planet")
    cmd.append(str(pbf))

    for attempt in range(1, SETTINGS.PBF_MAX_UPDATE_PASSES + 1):
        BUILD_LOGGER.info(f"Updating {pbf}, pass {attempt}")
        returncode = _run(cmd, check=False)
        if returncode == 0:
            BUILD_LOGGER.info(f"{pbf} is up to date.")
            return
        if returncode != 1:
            raise BuildError(f"pyosmium-up-to-date failed with exit code {returncode}")
        BUILD_LOGGER.info("Size cap reached, more diffs are available.")

    raise BuildError(
        f"{pbf} is still behind after {SETTINGS.PBF_MAX_UPDATE_PASSES} passes, "
        "raise PBF_MAX_UPDATE_PASSES or PBF_UPDATE_SIZE_MB."
    )


def build_graph(generations_dir: Path, pbf: Path) -> Path:
    """
    Builds a Valhalla tile set into a fresh generation directory.

    Nothing existing is ever overwritten: every build writes into a new directory, so no reader
    can be looking at what is being written.

    :param generations_dir: the directory holding every built generation.
    :param pbf: the OSM PBF to build from.

    :returns: the generation directory holding the finished tile set.
    """
    BUILD_STATUS.stage(BuildStage.BUILDING_TILES)
    generation = generations_dir.joinpath(datetime.now(timezone.utc).strftime(GENERATION_FORMAT))
    generation.mkdir(parents=True)
    create_lock_file(generation)
    BUILD_STATUS.generation(generation.name)

    elevation_dir = SETTINGS.get_elevation_dir()
    elevation_dir.mkdir(parents=True, exist_ok=True)

    config_path = generation.joinpath("valhalla.json")
    BUILD_LOGGER.info(f"Building valhalla.json in {generation}")
    with open(config_path, "w") as fh:
        _run(
            [
                _binary("valhalla_build_config"),
                "--mjolnir-tile-extract",
                "",
                "--mjolnir-tile-dir",
                str(generation),
                "--additional-data-elevation",
                str(elevation_dir),
                "--mjolnir-concurrency",
                str(SETTINGS.CONCURRENCY),
                "--mjolnir-max-cache-size",
                str(SETTINGS.MAX_CACHE_SIZE),
                "--logging-type",
                "std_out",
                "--logging-color",
                "false",
            ],
            stdout=fh,
        )

    _run([
        _binary("valhalla_build_tiles"),
        "-c",
        str(config_path),
        "-s",
        "initialize",
        "-e",
        "build",
        str(pbf),
    ])

    if SETTINGS.USE_ELEVATION:
        BUILD_STATUS.stage(BuildStage.BUILDING_ELEVATION)
        _run([
            _binary("valhalla_build_elevation"),
            "--from-tiles",  # makes sure we only download the elevation tiles we need
            "--decompress",
            "-c",
            str(config_path),
            "-v",
        ])
        BUILD_STATUS.stage(BuildStage.ENHANCING_TILES)
        _run([
            _binary("valhalla_build_tiles"),
            "-c",
            str(config_path),
            "-s",
            "enhance",
            "-e",
            "cleanup",
            str(pbf),
        ])
    else:
        BUILD_LOGGER.warning(
            "USE_ELEVATION is off, so the enhance and cleanup stages are skipped, as they were "
            "in scripts/run_valhalla.sh."
        )

    return generation


def write_build_meta(generation: Path, pbf: Path) -> Path:
    """
    Records what went into a generation, for the health endpoint to serve.

    :param generation: the finished generation directory.
    :param pbf: the PBF the tiles were built from.

    :returns: the path to the written metadata.
    """
    meta = {
        "generation": generation.name,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "pbf_path": str(pbf),
        "pbf_modified": datetime.fromtimestamp(pbf.stat().st_mtime, timezone.utc).isoformat(),
        "elevation": SETTINGS.USE_ELEVATION,
        "valhalla_version": _valhalla_version(),
    }
    meta_path = generation.joinpath("build_meta.json")
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf8")

    return meta_path


def swap_graph_link(link: Path, generation: Path) -> None:
    """
    Points the graph symlink at a generation with a single atomic rename.

    ``ln -sfn`` unlinks before it symlinks and leaves the path dangling in between, so the
    replacement is staged next to the link and moved onto it with ``rename(2)`` instead.

    :param link: the graph symlink.
    :param generation: the generation it should point at.
    """
    BUILD_STATUS.stage(BuildStage.SWAPPING)
    # make sure the directory exists
    link.parent.mkdir(parents=True, exist_ok=True)
    staged = link.with_name(link.name + ".tmp")

    # dangling symlink, just remove it
    if staged.is_symlink() or staged.exists():
        staged.unlink()

    staged.symlink_to(generation)
    os.replace(staged, link)
    BUILD_LOGGER.info(f"{link} now points at {generation.name}.")
