"""
Answers whether this app instance should be sent packaging work.

The question a readiness probe asks is "should traffic reach this pod", so the checks here are scoped
to what the app container itself needs to turn a submitted job into a queued one that a worker can
finish: a graph to package from, the database holding jobs and locks, the queue to enqueue onto, and
somewhere to write the package.

Two things are deliberately *not* checked. Whether a worker is alive belongs to the worker's own
container: failing readiness here would pull the HTTP API out of its service, so nobody could even ask
why their job was stuck. And a graph build in progress is not a problem at all, because a build writes
into a fresh generation and leaves the current one serving until it swaps the symlink.

``/api/v1/health`` is the other half of this: authenticated, detailed, and always 200. It reports the
worker, the build, and which of these checks failed. This endpoint is the machine-readable signal, so
it carries everything in the status code and gives an anonymous caller a single bit.
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
from ...utils.file_utils import resolve_graph

router = APIRouter()


def _graph_ready() -> bool:
    return resolve_graph() is not None


def _postgres_ready(db: Session) -> bool:
    try:
        db.execute(text("SELECT 1"))
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

    Public on purpose: a probe has no good way to send credentials, and the body is a single bit the
    caller could infer from the status code anyway.

    :returns: 200 while ready, 503 otherwise.
    """
    pool: ArqRedis | None = getattr(req.app.state, "redis_pool", None)
    ready = _graph_ready() and _postgres_ready(db) and _output_ready() and await _redis_ready(pool)

    return JSONResponse(
        content={"ready": ready},
        status_code=HTTP_200_OK if ready else HTTP_503_SERVICE_UNAVAILABLE,
    )
