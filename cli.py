import asyncio
import sys
from argparse import ArgumentParser

from arq import create_pool
from arq.connections import RedisSettings

from routing_packager_app import SETTINGS

description = "Enqueues a re-creation of every package flagged for updating."
parser = ArgumentParser(description=description)


async def enqueue_update() -> None:
    pool = await create_pool(RedisSettings.from_dsn(SETTINGS.REDIS_URL))
    try:
        await pool.enqueue_job("update_all_packages")
    finally:
        await (getattr(pool, "aclose", None) or pool.close)()


if __name__ == "__main__":
    parser.parse_args()
    asyncio.run(enqueue_update())
    print("Enqueued update_all_packages.", file=sys.stderr)
