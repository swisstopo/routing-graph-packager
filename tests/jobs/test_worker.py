from copy import deepcopy
from pathlib import Path
import shutil
from zipfile import ZipFile

import pytest
from starlette.exceptions import HTTPException
from starlette.testclient import TestClient

from routing_packager_app import SETTINGS
from routing_packager_app.worker import create_package

from ..utils_ import create_new_job, create_package_params

DEFAULT_ARGS = {
    "ctx": "",
    "job_id": 1,
    "job_name": "test",
    "description": "test desc",
    "bbox": "5.9559,45.818,10.4921,47.8084",
    "zip_path": str(SETTINGS.get_output_path().joinpath("test", "test.zip")),
    "user_id": 1,
}


@pytest.mark.asyncio
async def test_success(get_client: TestClient, basic_auth_header, copy_valhalla_tiles):
    # create the right bbox
    bbox = "1.486630,42.608695,1.534706,42.646334"
    args = deepcopy(DEFAULT_ARGS)
    args["bbox"] = bbox
    new_job = create_new_job(get_client, args, basic_auth_header)
    shutil.rmtree(Path(new_job.json()["zip_path"]).parent)
    params = create_package_params(new_job.json())

    await create_package(*params)

    out_fp = Path(new_job.json()["zip_path"])
    with ZipFile(out_fp, "r") as zip:
        zip_dir = zip.namelist()
        assert "valhalla_tiles/2/000/763/926.gph" in zip_dir
        assert "valhalla_tiles/2/000/763/925.gph" in zip_dir
        assert "valhalla_tiles/1/047/701.gph" in zip_dir
        assert "valhalla_tiles/0/003/015.gph" in zip_dir
    assert out_fp.is_file() is True
    assert out_fp.stat().st_size == 707166


@pytest.mark.asyncio
async def test_fail_no_graph(get_client: TestClient, basic_auth_header):
    new_job = create_new_job(get_client, DEFAULT_ARGS, basic_auth_header)
    shutil.rmtree(Path(new_job.json()["zip_path"]).parent)
    params = create_package_params(new_job.json())
    with pytest.raises(HTTPException) as e:
        await create_package(*params)

    assert e.value.status_code == 500
    assert "No graph available behind" in e.value.detail


@pytest.mark.asyncio
async def test_fail_no_tiles_in_dir(get_client: TestClient, basic_auth_header, empty_graph):
    new_job = create_new_job(get_client, DEFAULT_ARGS, basic_auth_header)
    shutil.rmtree(Path(new_job.json()["zip_path"]).parent)
    params = create_package_params(new_job.json())
    with pytest.raises(HTTPException) as e:
        await create_package(*params)

    assert e.value.status_code == 404
    assert "No Valhalla tiles in" in e.value.detail


@pytest.mark.asyncio
async def test_fail_no_tiles_in_bbox(get_client: TestClient, basic_auth_header, copy_valhalla_tiles):
    new_job = create_new_job(get_client, DEFAULT_ARGS, basic_auth_header)
    shutil.rmtree(Path(new_job.json()["zip_path"]).parent)
    params = create_package_params(new_job.json())
    with pytest.raises(HTTPException) as e:
        await create_package(*params)

    assert e.value.status_code == 404
    assert "No Valhalla tiles in bbox" in e.value.detail
