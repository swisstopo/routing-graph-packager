import fcntl
import os
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Set


def make_package_path(base_dir: Path, name: str, provider: str) -> Path:
    """
    Returns the ZIP file name from DATA_DIR, provider and dataset name.

    :param base_dir: The DATA_DIR env variable
    :param name: The dataset name, e.g. moldavia
    :param provider: The provider's name, e.g. osm

    :returns: The full path to the data package
    """
    file_name = "_".join([provider, name])

    # also create a folder with the same name
    out_dir = base_dir.joinpath(file_name)
    out_dir.mkdir(parents=True)

    return out_dir.joinpath(file_name + ".zip").resolve()


def make_zip(source_paths: Set[Path], parent_path: Path, out_fp: str):
    """
    ZIPs the input paths.

    :param source_paths: set of paths which need zipping.
    :param parent_path: the valhalla_tiles dir, for the file's arcname.
    :param out_fp: full path to the resulting Zip file.
    """
    with zipfile.ZipFile(out_fp, "w", zipfile.ZIP_DEFLATED) as archive:
        for p in source_paths:
            archive.write(p, "valhalla_tiles/" + str(p.relative_to(parent_path)))


LOCK_NAME = ".lock"
LOCK_POLL_INTERVAL = 5.0
RESOLVE_ATTEMPTS = 2


def create_lock_file(directory: Path) -> Path:
    """
    Creates the advisory lock file readers and the pruner synchronise on.

    :param directory: the graph generation directory.

    :returns: the full path to the lock file.
    """
    lock = directory.joinpath(LOCK_NAME)
    lock.touch(exist_ok=True)
    lock.chmod(0o644)

    return lock


@contextmanager
def lock_generation_shared(link: Path) -> Iterator[Path]:
    """
    Resolves the graph symlink and holds a shared lock on the generation it points at.

    The lock is advisory: it only fences the pruner in the graph build container, which takes
    an exclusive lock before deleting a generation. Holding it guarantees the resolved
    directory survives for as long as the caller reads from it.

    Resolving and locking are two separate syscalls, so the generation can in principle be
    pruned in between. That shows up in one of two ways, both recovered from by resolving the
    symlink again, at which point it points at a generation that is current and therefore
    never a prune candidate:

      - the lock file is already gone, so opening it raises ENOENT
      - the lock file was opened but unlinked before the lock was granted, leaving the open
        file description pointing at an inode with no remaining links

    A single re-resolve is always enough. The pruner only ever deletes generations the symlink
    does not point at, and only at the start of a build, while the symlink is only moved at the
    end of one. For a resolved generation to be pruned, a whole build therefore has to complete
    between these two adjacent syscalls.

    :param link: the graph symlink, e.g. tmp_data/osm/graph.

    :returns: the resolved generation directory.
    """
    error: OSError | None = None
    for _ in range(RESOLVE_ATTEMPTS):
        try:
            generation = link.resolve(strict=True)
            fd = os.open(generation.joinpath(LOCK_NAME), os.O_RDONLY)
        except OSError as e:
            error = e
            continue

        fcntl.flock(fd, fcntl.LOCK_SH)
        if not os.fstat(fd).st_nlink:
            os.close(fd)
            error = FileNotFoundError(f"Graph generation {generation} was pruned while locking it.")
            continue

        try:
            yield generation
        finally:
            os.close(fd)
        return

    raise error or FileNotFoundError(f"No graph generation behind {link}.")


@contextmanager
def lock_exclusive(lock_path: Path, timeout: float = 0.0) -> Iterator[bool]:
    """
    Takes an exclusive advisory lock on a file, creating it if needed.

    :param lock_path: the lock file.
    :param timeout: how many seconds to keep retrying for. Zero means a single attempt.

    :returns: whether the lock was acquired.

    Note that this locking mechanism works across containers sharing the same kernel
    since flock operates on the kernel level.
    """
    # for own sanity: make sure the directory exists
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # maybe create the file
    fd = os.open(lock_path, os.O_RDONLY | os.O_CREAT, 0o644)
    deadline = time.monotonic() + timeout
    try:
        acquired = False
        # keep trying until we hit the timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(LOCK_POLL_INTERVAL, remaining))
        yield acquired
    finally:
        os.close(fd)
