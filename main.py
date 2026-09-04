from contextlib import asynccontextmanager
import uvicorn as uvicorn
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI

from routing_packager_app import create_app
from routing_packager_app.constants import Providers
from routing_packager_app.db import create_tables, get_db
from routing_packager_app.config import SETTINGS
from routing_packager_app.metrics import start_metrics_server
from routing_packager_app.api_v1.models import User


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_metrics_server()
    create_tables()
    app.state.redis_pool = await create_pool(RedisSettings.from_dsn(SETTINGS.REDIS_URL))
    User.add_admin_user(next(get_db()))

    # create the directories
    for provider in Providers:
        p = SETTINGS.get_tmp_data_dir().joinpath(provider.lower())
        p.mkdir(exist_ok=True)
    SETTINGS.get_output_path().mkdir(exist_ok=True)
    yield
    await app.state.redis_pool.shutdown()


app: FastAPI = create_app(lifespan=lifespan)

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=5000, reload=True)
