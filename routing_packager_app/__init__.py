from typing import Optional
from fastapi import FastAPI
from starlette.types import Lifespan
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from pathlib import Path

from .api_v1 import api_v1_router
from .config import SETTINGS


def create_app(lifespan: Optional[Lifespan[FastAPI]]):
    """
    Creates a FastAPI app dynamically.
    """

    with open(SETTINGS.DESCRIPTION_PATH) as fh:
        description = fh.read()
    
    BASE_DIR = Path(__file__).resolve().parent.parent
    app = FastAPI(title="Routing Graph Packager App", description=description, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=f"{BASE_DIR}/static"), name="static")

    register_middlewares(app)
    register_router(app)

    return app


def register_router(app: FastAPI):
    app.include_router(api_v1_router, prefix="/api/v1")


def register_middlewares(app: FastAPI):
    # only from 1kb we'll do gzipping
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    if SETTINGS.CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=SETTINGS.CORS_ORIGINS,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
