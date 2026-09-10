"""
Tells the caller whether this app instance should be sent packaging work by means of the reponse code.

The more machine-readable and publicly accessible variant of ``/health``.
"""

import os
from typing import Any

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlmodel import Session
from starlette.status import HTTP_200_OK, HTTP_503_SERVICE_UNAVAILABLE

from ...config import SETTINGS
from ...db import get_db
from ...utils.file_utils import get_deployed_providers, resolve_graph

router = APIRouter()


def _any_graph_ready() -> bool:
    """
    Reports whether any deployed provider has a graph to package from.
    """
    return any(resolve_graph(provider) is not None for provider in get_deployed_providers())


def _postgres_ready(db: Session) -> bool:
    try:
        db.exec(text("SELECT 1"))
    except Exception:
        return False

    return True


async def _redis_ready(pool: ArqRedis | None) -> bool:
    if pool is None:
        return False

    try:
        await pool.ping()
    except Exception:
        return False

    return True


def _output_ready() -> bool:
    output = SETTINGS.get_output_path()

    return output.is_dir() and os.access(output, os.W_OK)


@router.get("", response_class=JSONResponse)
async def get_readiness(req: Request, db: Session = Depends(get_db)) -> Any:
    """
    Reports whether this instance can take packaging jobs.

    :returns: 200 while ready, 503 otherwise.
    """
    pool: ArqRedis | None = getattr(req.app.state, "redis_pool", None)
    ready = _any_graph_ready() and _postgres_ready(db) and _output_ready() and await _redis_ready(pool)

    return JSONResponse(
        content={"ready": ready},
        status_code=HTTP_200_OK if ready else HTTP_503_SERVICE_UNAVAILABLE,
    )
