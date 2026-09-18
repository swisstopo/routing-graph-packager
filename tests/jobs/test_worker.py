from copy import deepcopy
import logging
from pathlib import Path
import shutil
from zipfile import ZipFile

import pytest
from starlette.exceptions import HTTPException
from starlette.testclient import TestClient

from routing_packager_app import SETTINGS
from routing_packager_app.logger import LOGGER
from routing_packager_app.worker import create_package, update_all_packages

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


@pytest.mark.asyncio
async def test_failed_update_keeps_the_existing_package(get_client: TestClient, basic_auth_header):
    new_job = create_new_job(get_client, DEFAULT_ARGS, basic_auth_header)
    shutil.rmtree(Path(new_job.json()["zip_path"]).parent)
    params = create_package_params(new_job.json())
    previous = params.zip_path.parent
    previous.joinpath("osm_test.zip").write_bytes(b"the package from the last build")

    with pytest.raises(HTTPException):
        await create_package(*params, True)

    assert previous.is_dir()
    assert previous.joinpath("osm_test.zip").read_bytes() == b"the package from the last build"


@pytest.mark.asyncio
async def test_failed_creation_removes_the_partial_package(
    get_client: TestClient, basic_auth_header
):
    new_job = create_new_job(get_client, DEFAULT_ARGS, basic_auth_header)
    shutil.rmtree(Path(new_job.json()["zip_path"]).parent)
    params = create_package_params(new_job.json())
    partial = params.zip_path.parent

    with pytest.raises(HTTPException):
        await create_package(*params, False)

    assert not partial.exists()


@pytest.mark.asyncio
async def test_a_missing_output_directory_does_not_mask_the_failure(
    get_client: TestClient, basic_auth_header
):
    new_job = create_new_job(get_client, DEFAULT_ARGS, basic_auth_header)
    shutil.rmtree(Path(new_job.json()["zip_path"]).parent)
    params = create_package_params(new_job.json())
    shutil.rmtree(params.zip_path.parent)

    with pytest.raises(HTTPException) as e:
        await create_package(*params, False)

    assert e.value.status_code == 500


@pytest.mark.asyncio
async def test_update_all_packages_only_rebuilds_its_own_providers_packages(
    get_client: TestClient, basic_auth_header, monkeypatch
):
    """
    A build only moves one provider's graph, so the others' packages are still current.
    """
    # keep the SMTP handler out of it, the failures below would try to mail them
    monkeypatch.setattr(LOGGER, "handlers", [logging.NullHandler()])

    for provider in ("osm", "tomtom"):
        create_new_job(
            get_client,
            {**DEFAULT_ARGS, "name": f"upd{provider}", "provider": provider, "update": True},
            basic_auth_header,
        )

    assert (await update_all_packages({}, "osm"))["total"] == 1
    assert (await update_all_packages({}, "tomtom"))["total"] == 1
    assert (await update_all_packages({}, "here"))["total"] == 0


@pytest.mark.asyncio
async def test_update_all_packages_keeps_the_existing_package_when_it_fails(
    get_client: TestClient, basic_auth_header, monkeypatch
):
    monkeypatch.setattr(LOGGER, "handlers", [logging.NullHandler()])

    new_job = create_new_job(
        get_client, {**DEFAULT_ARGS, "update": True}, basic_auth_header
    )
    package_dir = Path(new_job.json()["zip_path"]).parent
    package_dir.joinpath("osm_test.zip").write_bytes(b"the package from the last build")

    result = await update_all_packages({}, "osm")

    assert result == {"total": 1, "succeeded": 0}
    assert package_dir.joinpath("osm_test.zip").read_bytes() == b"the package from the last build"
