import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple

from ..config import SETTINGS
from ..logger import BUILD_LOGGER
from ..utils.file_utils import LOCK_NAME, create_lock_file, lock_exclusive

GENERATION_FORMAT = "%Y%m%dT%H%M%S"


class BuildError(Exception):
    """Raised when a graph build step fails and the symlink must not be swapped."""


def _binary(name: str) -> str:
    candidate = Path(sys.executable).parent.joinpath(name)
    if candidate.is_file():
        return str(candidate)

    return shutil.which(name) or name


def _run(cmd: List[str], **kwargs) -> None:
    BUILD_LOGGER.info(f"Running {' '.join(cmd)}")
    completed = subprocess.run(cmd, **kwargs)
    if completed.returncode != 0:
        raise BuildError(f"'{cmd[0]}' failed with exit code {completed.returncode}")


def _valhalla_version() -> str:
    try:
        out = subprocess.run(
            [_binary("valhalla_build_tiles"), "--version"], capture_output=True, text=True, timeout=30
        )
        return (out.stdout or out.stderr).strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def prune_generations(generations_dir: Path, link: Path, keep: int) -> Tuple[List[Path], List[Path]]:
    """
    Deletes graph generations that are neither current nor still held by a packaging job.

    This is the only place tiles are ever deleted. A generation is only removed once an
    exclusive lock on its lock file is granted, which fences it against workers holding a
    shared lock for the duration of a zip.

    :param generations_dir: the directory holding every built generation.
    :param link: the graph symlink, whose target is never pruned.
    :param keep: how many generations to retain, the current one included.

    :returns: the pruned and the skipped generation directories.
    """
    if not generations_dir.is_dir():
        return [], []

    current = link.resolve() if link.is_symlink() else None
    others = sorted((p for p in generations_dir.iterdir() if p.is_dir() and p != current), reverse=True)
    retained = set(others[: max(keep - 1, 0)])

    pruned: List[Path] = []
    skipped: List[Path] = []
    for generation in others:
        if generation in retained:
            continue
        with lock_exclusive(generation.joinpath(LOCK_NAME)) as acquired:
            if not acquired:
                BUILD_LOGGER.info(f"Generation {generation.name} is held by a reader, skipping prune.")
                skipped.append(generation)
                continue
            shutil.rmtree(generation, ignore_errors=True)
            BUILD_LOGGER.info(f"Pruned generation {generation.name}.")
            pruned.append(generation)

    return pruned, skipped


def download_pbf(pbf: Path) -> None:
    """
    Downloads the configured PBF to its local path.

    :param pbf: where the PBF should end up.
    """
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
    """
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
        returncode = subprocess.run(cmd).returncode
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
    generation = generations_dir.joinpath(datetime.now(timezone.utc).strftime(GENERATION_FORMAT))
    generation.mkdir(parents=True)
    create_lock_file(generation)

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
                "--mjolnir-logging-type",
                "",
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
        _run([
            _binary("valhalla_build_elevation"),
            "--from-tiles",
            "--decompress",
            "-c",
            str(config_path),
            "-v",
        ])
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
    link.parent.mkdir(parents=True, exist_ok=True)
    staged = link.with_name(link.name + ".tmp")
    if staged.is_symlink() or staged.exists():
        staged.unlink()
    staged.symlink_to(generation)
    os.replace(staged, link)
    BUILD_LOGGER.info(f"{link} now points at {generation.name}.")
