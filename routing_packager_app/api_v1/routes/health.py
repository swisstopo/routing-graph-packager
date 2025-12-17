from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasicCredentials
from starlette.status import HTTP_404_NOT_FOUND
import requests

from ..auth import BasicAuth, HeaderKey
from ...config import SETTINGS

router = APIRouter()

@router.get("", response_class=JSONResponse)
def get_health(
    auth: HTTPBasicCredentials = Depends(BasicAuth),
    key: str = Depends(HeaderKey),
):
    result = {"valhalla": {"8002": HTTP_404_NOT_FOUND, "8003": HTTP_404_NOT_FOUND}}

    for port in (8002, 8003):
        try:
            r = requests.get(f"{SETTINGS.VALHALLA_URL}:{port}/status", timeout=2)
            result["valhalla"][str(port)] = r.status_code
        except requests.exceptions.RequestException:
            pass  # keep default 404 when unreachable

    return result
