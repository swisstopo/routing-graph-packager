import json
from datetime import datetime, timezone
from hmac import compare_digest
from typing import Any, Dict

from arq.connections import ArqRedis
from arq.constants import default_queue_name, health_check_key_suffix
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
from ..models import APIKeys, APIPermission, User

router = APIRouter()

STALE_AFTER = 120.0
WORKER_HEALTH_KEY = default_queue_name + health_check_key_suffix


def _is_admin(auth: HTTPBasicCredentials | None) -> bool:
    """
    Checks the basic auth credentials against the configured admin without touching the database.

    ``User.add_admin_user`` seeds the admin row from these same two settings at startup, so this
    is not a separate credential. It exists so that the endpoint can still answer, and report
    Postgres as down, when Postgres is the thing that is broken.

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
    if _is_admin(auth):
        return True

    try:
        return bool(APIKeys.check_key(db, key, APIPermission.INTERNAL)) or bool(User.get_user(db, auth))
    except Exception:
        return False


def _graph_report() -> Dict[str, Any]:
    link = SETTINGS.get_graph_link()
    report: Dict[str, Any] = {"available": False, "path": str(link)}

    if not link.is_symlink():
        return report

    try:
        generation = link.resolve(strict=True)
        meta = json.loads(generation.joinpath("build_meta.json").read_text(encoding="utf8"))
    except (OSError, ValueError):
        return report

    return {"available": True, "path": str(link), **meta}


def _build_report() -> Dict[str, Any]:
    try:
        report = json.loads(SETTINGS.get_build_status_path().read_text(encoding="utf8"))
    except (OSError, ValueError):
        return {"state": BuildState.UNKNOWN.value, "stale": False}

    report["stale"] = _is_stale(report)

    return report


def _is_stale(report: Dict[str, Any]) -> bool:
    """
    Decides whether a build that claims to be running still is.

    The builder refreshes ``updated_at`` while it works, so a running build is never more than a
    few heartbeats old. A container killed mid-build leaves its last stage behind forever, which
    is what this catches.

    :param report: the parsed build status file.
    """
    if report.get("state") != BuildState.BUILDING.value:
        return False

    try:
        updated_at = datetime.fromisoformat(report["updated_at"])
    except (KeyError, TypeError, ValueError):
        return True

    return (datetime.now(timezone.utc) - updated_at).total_seconds() > STALE_AFTER


def _postgres_report(db: Session) -> Dict[str, Any]:
    try:
        db.execute(text("SELECT 1"))
    except Exception as e:
        return {"up": False, "error": str(e)}

    return {"up": True, "error": None}


def _parse_worker_health(raw: bytes) -> Dict[str, Any]:
    """
    Pulls the counters out of the health check string ARQ's worker writes.

    The value looks like ``Aug-25 11:41:20 j_complete=0 j_failed=0 j_retried=0 j_ongoing=0
    queued=0``. Its timestamp carries neither a year nor a zone, so it is reported verbatim
    rather than parsed - the key's presence already answers whether the worker is alive, since
    the worker sets it with a TTL.

    :param raw: the health check key's value.
    """
    fields = raw.decode(errors="replace").split()
    counters = dict(field.split("=", 1) for field in fields if "=" in field)

    def number(name: str) -> int | None:
        try:
            return int(counters[name])
        except (KeyError, ValueError):
            return None

    return {
        "last_report": " ".join(field for field in fields if "=" not in field) or None,
        "ongoing": number("j_ongoing"),
        "complete": number("j_complete"),
        "failed": number("j_failed"),
        "retried": number("j_retried"),
    }


async def _services_report(db: Session, pool: ArqRedis | None) -> Dict[str, Any]:
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
        worker["queued"] = await pool.zcard(default_queue_name)
        if raw:
            worker.update(_parse_worker_health(raw), up=True)
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
    if not _authenticate(db, auth, key):
        raise HTTPException(
            HTTP_401_UNAUTHORIZED,
            "No valid authentication method provided. Possible authentication methods: API key"
            "(x-api-key header) username/password (basic auth).",
        )

    graph = _graph_report()
    build = _build_report()
    services = await _services_report(db, getattr(req.app.state, "redis_pool", None))

    healthy = (
        graph["available"]
        and services["postgres"]["up"]
        and services["redis"]["up"]
        and services["worker"]["up"]
    )

    return {
        "status": "ok" if healthy else "degraded",
        "graph": graph,
        "build": build,
        "services": services,
    }
