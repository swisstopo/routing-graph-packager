import argparse
import asyncio
import signal
import sys
import time
from datetime import datetime, timezone
from typing import List

from arq import create_pool
from arq.connections import RedisSettings
from croniter import croniter

from ..config import SETTINGS
from ..constants import PROVIDERS, BuildOutcome
from ..db import create_tables
from ..logger import BUILD_LOGGER
from ..metrics import STATSD
from ..utils.lock_utils import lock_exclusive
from .status import BUILD_STATUS, EXTERNAL_SCHEDULE
from .builder import (
    BuildError,
    build_graph,
    download_pbf,
    prune_generations,
    swap_graph_link,
    terminate_current,
    update_pbf,
    write_build_meta,
)

SLEEP_CHUNK = 30  # can be interrupted every 30 seconds

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_LOCKED = 75

_stop = False


def _handle_signal(signum, _frame) -> None:
    global _stop
    _stop = True
    BUILD_LOGGER.info(f"Received signal {signum}, stopping after the current step.")
    terminate_current()


def _next_build_at() -> datetime:
    return croniter(SETTINGS.GRAPH_BUILD_CRON, datetime.now(timezone.utc)).get_next(datetime)


def _sleep_until(when: datetime) -> None:
    BUILD_LOGGER.info(f"Next graph build at {when.isoformat()}.")
    while not _stop:
        remaining = (when - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, SLEEP_CHUNK))


async def _enqueue_package_updates(provider: str) -> None:
    """
    After a provider's graph build finishes, recreate existing packages with the
    new graph data.

    :param provider: the dataset provider whose graph was just swapped in.
    """
    pool = await create_pool(RedisSettings.from_dsn(SETTINGS.REDIS_URL))
    try:
        await pool.enqueue_job("update_all_packages", provider)
        BUILD_LOGGER.info(f"Enqueued update_all_packages for {provider} packages.")
    finally:
        await (getattr(pool, "aclose", None) or pool.close)()


def run_build(provider: str) -> BuildOutcome:
    """
    Runs one full build: prune, update the PBF, build a generation and swap it in.

    :param provider: the dataset provider to build for.

    :returns: whether a graph was built or the run gave way to a concurrent build.
    """
    link = SETTINGS.get_graph_link(provider)
    generations_dir = SETTINGS.get_generations_dir(provider)
    generations_dir.mkdir(parents=True, exist_ok=True)
    pbf = SETTINGS.get_pbf_path(provider)

    started = time.perf_counter()
    outcome = "failed"

    try:
        # get a lock on the build directory, making sure there isn't another
        # graph build going on currently
        with lock_exclusive(SETTINGS.get_provider_dir(provider)) as acquired:
            if not acquired:
                BUILD_LOGGER.warning("Another graph build holds the build lock, skipping this run.")
                outcome = "skipped"
                return BuildOutcome.SKIPPED

            prune_generations(generations_dir, link, SETTINGS.GRAPH_PRUNE_TIMEOUT)

            if not pbf.is_file():
                download_pbf(pbf)
            else:
                update_pbf(pbf)

            generation = build_graph(generations_dir, pbf)
            write_build_meta(generation, pbf)
            swap_graph_link(link, generation)

        BUILD_STATUS.idle()
        outcome = "succeeded"
    finally:
        tags = [f"outcome:{outcome}", f"provider:{provider}"]
        STATSD.timing("build.duration", (time.perf_counter() - started) * 1000, tags=tags)
        STATSD.increment(f"build.{outcome}", tags=[f"provider:{provider}"])

    # the build ran, so its time to re-create
    # existing packages with the new data
    asyncio.run(_enqueue_package_updates(provider))

    return BuildOutcome.BUILT


def run_once(provider: str) -> int:
    """
    Runs exactly one build and reports its result as an exit code.

    This is the entry point for an external scheduler, which
    owns the schedule and expects the container to do one unit of work and exit.
    ``GRAPH_BUILD_CRON`` is ignored.

    :param provider: the dataset provider to build for.

    :returns: ``EXIT_OK`` when a graph was built and swapped in, ``EXIT_LOCKED`` when another
        build was already running and ``EXIT_FAILED`` when the build failed.
    """
    BUILD_LOGGER.info(f"Running a single {provider} graph build.")

    try:
        outcome = run_build(provider)
    except BuildError as e:
        BUILD_LOGGER.critical(f"Graph build failed, keeping the current graph: {e}")
        BUILD_STATUS.failed(str(e))
        return EXIT_FAILED
    except Exception as e:  # pragma: no cover
        BUILD_LOGGER.critical(f"Graph build failed unexpectedly, keeping the current graph: {e}")
        BUILD_STATUS.failed(str(e))
        return EXIT_FAILED

    if outcome is BuildOutcome.SKIPPED:
        return EXIT_LOCKED

    BUILD_STATUS.idle(EXTERNAL_SCHEDULE)

    return EXIT_OK


def run_scheduled(provider: str) -> int:
    """
    Builds on the ``GRAPH_BUILD_CRON`` schedule until the process is asked to stop.

    :param provider: the dataset provider to build for.

    :returns: ``EXIT_OK``, or ``EXIT_FAILED`` if the cron expression is unusable.
    """
    if not croniter.is_valid(SETTINGS.GRAPH_BUILD_CRON):
        BUILD_LOGGER.critical(
            f"GRAPH_BUILD_CRON '{SETTINGS.GRAPH_BUILD_CRON}' is not a valid cron expression."
        )
        return EXIT_FAILED

    BUILD_LOGGER.info(
        f"{provider} graph builder started, GRAPH_BUILD_CRON is '{SETTINGS.GRAPH_BUILD_CRON}'."
    )

    if not SETTINGS.get_graph_link(provider).is_symlink():
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

    return EXIT_OK


def _parse_args(argv: List[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m routing_packager_app.graph_build",
        description="Builds the Valhalla graph the packager creates its extracts from.",
    )
    parser.add_argument(
        "--provider",
        choices=PROVIDERS,
        help=f"Which provider to build for. Must be one of {', '.join(PROVIDERS)}.",
        required=True,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Build once and exit, for an external scheduler such as a Kubernetes CronJob, "
        "rather than looping on GRAPH_BUILD_CRON.",
    )

    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    args = _parse_args(argv)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    provider = args.provider
    # BUILD_STATUS is a singleton, set its provider once here
    BUILD_STATUS.bind(provider)
    # create the provider directory
    SETTINGS.get_provider_dir(provider).mkdir(parents=True, exist_ok=True)
    create_tables()

    if args.once:
        return run_once(provider)

    return run_scheduled(provider)


if __name__ == "__main__":
    sys.exit(main())
