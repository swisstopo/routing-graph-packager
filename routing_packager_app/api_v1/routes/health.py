import json
from hmac import compare_digest
from typing import Any, Dict

from arq.connections import ArqRedis
from arq.constants import default_queue_name
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasicCredentials
from sqlalchemy import text
from sqlmodel import Session
from starlette.status import HTTP_401_UNAUTHORIZED

from ..auth import BasicAuth, HeaderKey
from ...config import SETTINGS
from ...constants import BuildState
from ...db import get_db
from ...metrics import WORKER_HEALTH_KEY, parse_worker_health
from ..models import APIKeys, APIPermission, User
from ...utils.file_utils import get_deployed_providers, resolve_graph

router = APIRouter()


def _is_admin(auth: HTTPBasicCredentials | None) -> bool:
    """
    Checks the basic auth credentials against the configured admin without touching the database.

    It exists so that the endpoint can still answer, and report
    the database as down.

    :param auth: the decoded basic auth header, if one was sent.
    """
    if not auth or not auth.username or not auth.password:
        return False

    return compare_digest(auth.username.encode(), SETTINGS.ADMIN_EMAIL.encode()) and compare_digest(
        auth.password.encode(), SETTINGS.ADMIN_PASS.encode()
    )


def _authenticate(db: Session, auth: HTTPBasicCredentials | None, key: str) -> bool:
    """
    Resolves whether the caller may read the health report.

    :param db: the database session used by the two database backed methods.
    :param auth: the decoded basic auth header, if one was sent.
    :param key: the x-api-key header's value, if one was sent.
    """

    # first check via user/pw combination
    # in order to still report health when the DB is down
    if _is_admin(auth):
        return True

    try:
        return bool(APIKeys.check_key(db, key, APIPermission.INTERNAL)) or bool(User.get_user(db, auth))
    except Exception:
        return False


def _graph_report(provider: str) -> Dict[str, Any]:
    link = SETTINGS.get_graph_link(provider)
    report: Dict[str, Any] = {"available": False, "path": str(link)}

    generation = resolve_graph(provider)
    if generation is None:
        return report

    try:
        meta = json.loads(generation.joinpath("build_meta.json").read_text(encoding="utf8"))
    except (OSError, ValueError):
        return report

    return {"available": True, "path": str(link), **meta}


def _build_report(provider: str) -> Dict[str, Any]:
    """
    Report on one provider's graph build.
    """
    try:
        return json.loads(SETTINGS.get_build_status_path(provider).read_text(encoding="utf8"))
    except (OSError, ValueError):
        return {"state": BuildState.UNKNOWN.value}


def _postgres_report(db: Session) -> Dict[str, Any]:
    """
    Minimal postgres smoke test.
    """
    try:
        db.exec(text("SELECT 1"))
    except Exception as e:
        return {"up": False, "error": str(e)}

    return {"up": True, "error": None}


async def _services_report(db: Session, pool: ArqRedis | None) -> Dict[str, Any]:
    """
    Report health of redis and postgres
    """
    worker: Dict[str, Any] = {
        "up": False,
        "last_report": None,
        "queued": None,
        "ongoing": None,
        "complete": None,
        "failed": None,
        "retried": None,
    }

    if pool is None:
        redis = {"up": False, "error": "No Redis pool on the application state."}
        return {"postgres": _postgres_report(db), "redis": redis, "worker": worker}

    try:
        await pool.ping()
        redis = {"up": True, "error": None}
    except Exception as e:
        redis = {"up": False, "error": str(e)}
        return {"postgres": _postgres_report(db), "redis": redis, "worker": worker}

    try:
        raw = await pool.get(WORKER_HEALTH_KEY)
        # zcard is a standard redis command
        # that reports the number of members in a sorted
        # set
        worker["queued"] = await pool.zcard(default_queue_name)
        if raw:
            worker.update(parse_worker_health(raw), up=True)
    except Exception:
        pass

    return {"postgres": _postgres_report(db), "redis": redis, "worker": worker}


@router.get("", response_class=JSONResponse)
async def get_health(
    req: Request,
    db: Session = Depends(get_db),
    auth: HTTPBasicCredentials = Depends(BasicAuth),
    key: str = Depends(HeaderKey),
):
    providers: Dict[str, Any] = {}
    any_graph_available = False
    for provider in get_deployed_providers():
        graph = _graph_report(provider)
        providers[provider] = {"graph": graph, "build": _build_report(provider)}

        # the instance is healthy as long as it can serve at least one provider
        any_graph_available = any_graph_available or graph["available"]

    services = await _services_report(db, getattr(req.app.state, "redis_pool", None))

    healthy = (
        any_graph_available
        and services["postgres"]["up"]
        and services["redis"]["up"]
        and services["worker"]["up"]
    )

    return {
        "status": "ok" if healthy else "degraded",
        "providers": providers,
        "services": services,
    }
