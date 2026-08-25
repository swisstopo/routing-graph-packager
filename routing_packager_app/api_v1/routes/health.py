import json

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasicCredentials

from ..auth import BasicAuth, HeaderKey
from ...config import SETTINGS

router = APIRouter()


@router.get("", response_class=JSONResponse)
def get_health(
    auth: HTTPBasicCredentials = Depends(BasicAuth),
    key: str = Depends(HeaderKey),
):
    link = SETTINGS.get_graph_link()
    result = {"graph": {"available": False, "path": str(link)}}

    if not link.is_symlink():
        return result

    try:
        generation = link.resolve(strict=True)
        meta = json.loads(generation.joinpath("build_meta.json").read_text(encoding="utf8"))
    except (OSError, ValueError):
        return result

    result["graph"] = {"available": True, "path": str(link), **meta}

    return result
