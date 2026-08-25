import asyncio
import signal
import sys
import time
from datetime import datetime, timezone

from arq import create_pool
from arq.connections import RedisSettings
from croniter import croniter

from ..config import SETTINGS
from ..constants import Providers
from ..logger import BUILD_LOGGER
from ..utils.file_utils import lock_exclusive
from .status import BUILD_STATUS
from .builder import (
    BuildError,
    build_graph,
    download_pbf,
    prune_generations,
    swap_graph_link,
    update_pbf,
    write_build_meta,
)

SLEEP_CHUNK = 30

_stop = False


def _handle_signal(signum, _frame) -> None:
    global _stop
    _stop = True
    BUILD_LOGGER.info(f"Received signal {signum}, stopping after the current step.")


def _next_build_at() -> datetime:
    return croniter(SETTINGS.GRAPH_BUILD_CRON, datetime.now(timezone.utc)).get_next(datetime)


def _sleep_until(when: datetime) -> None:
    BUILD_LOGGER.info(f"Next graph build at {when.isoformat()}.")
    while not _stop:
        remaining = (when - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, SLEEP_CHUNK))


async def _enqueue_package_updates() -> None:
    """
    After a graph build finishes, recreate existing packages with the
    new graph data.
    """
    pool = await create_pool(RedisSettings.from_dsn(SETTINGS.REDIS_URL))
    try:
        await pool.enqueue_job("update_all_packages")
        BUILD_LOGGER.info("Enqueued update_all_packages for the worker.")
    finally:
        await (getattr(pool, "aclose", None) or pool.close)()


def run_build(provider: str) -> None:
    """
    Runs one full build: prune, update the PBF, build a generation and swap it in.

    :param provider: the dataset provider to build for.
    """
    link = SETTINGS.get_graph_link(provider)
    generations_dir = SETTINGS.get_generations_dir(provider)
    generations_dir.mkdir(parents=True, exist_ok=True)
    pbf = SETTINGS.get_pbf_path()

    # get a lock on the build directory, making sure there isn't another
    # graph build going on currently
    with lock_exclusive(SETTINGS.get_build_lock_path(provider)) as acquired:
        if not acquired:
            BUILD_LOGGER.warning("Another graph build holds the build lock, skipping this run.")
            return

        prune_generations(
            generations_dir,
            link,
            SETTINGS.GRAPH_KEEP_GENERATIONS,
            SETTINGS.GRAPH_PRUNE_TIMEOUT,
        )

        if not pbf.is_file():
            download_pbf(pbf)
        else:
            update_pbf(pbf)

        generation = build_graph(generations_dir, pbf)
        write_build_meta(generation, pbf)
        swap_graph_link(link, generation)

    BUILD_STATUS.idle()

    # the build ran, so its time to re-create
    # existing packages with the new data
    asyncio.run(_enqueue_package_updates())


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    if not croniter.is_valid(SETTINGS.GRAPH_BUILD_CRON):
        BUILD_LOGGER.critical(
            f"GRAPH_BUILD_CRON '{SETTINGS.GRAPH_BUILD_CRON}' is not a valid cron expression."
        )
        return 1

    provider = Providers.OSM.lower()
    SETTINGS.get_provider_dir(provider).mkdir(parents=True, exist_ok=True)
    link = SETTINGS.get_graph_link(provider)

    BUILD_LOGGER.info(f"Graph builder started, GRAPH_BUILD_CRON is '{SETTINGS.GRAPH_BUILD_CRON}'.")

    if not link.is_symlink():
        BUILD_LOGGER.info("No graph generation available yet, building immediately.")
    else:
        _sleep_until(_next_build_at())

    # keep trying to run a single build
    # this while loop just tries to acquire an exclusive lock on
    # the build directory to make sure there is not another graph
    # build going on.
    # the global _stop is used to handle incoming signals to be able to
    # terminate manually
    while not _stop:
        try:
            run_build(provider)
        except BuildError as e:
            BUILD_LOGGER.critical(f"Graph build failed, keeping the current graph: {e}")
            BUILD_STATUS.failed(str(e))
        except Exception as e:  # pragma: no cover
            BUILD_LOGGER.critical(f"Graph build failed unexpectedly, keeping the current graph: {e}")
            BUILD_STATUS.failed(str(e))

        if _stop:
            break

        when = _next_build_at()
        BUILD_STATUS.idle(when)
        _sleep_until(when)

    BUILD_LOGGER.info("Graph builder stopped.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
