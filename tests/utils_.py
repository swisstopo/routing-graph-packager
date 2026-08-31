import json
from shutil import rmtree
from typing import List, Tuple  # noqa: F401

from starlette.testclient import TestClient

from httpx import Response
from routing_packager_app import SETTINGS
from routing_packager_app.graph_build.builder import swap_graph_link
from routing_packager_app.utils.file_utils import make_package_path

DEFAULT_ARGS_POST = {
    "name": "test",
    "description": "test description",
    "bbox": "0,0,2,2",
    "provider": "osm",
}


class FakePool:
    """Stands in for the ArqRedis pool the app puts on its state at startup."""

    def __init__(self, health=None, queued=0, fails=False):
        self.health = health
        self.queued = queued
        self.fails = fails

    async def ping(self):
        if self.fails:
            raise ConnectionError("Redis is unreachable")
        return True

    async def get(self, _key):
        return self.health

    async def zcard(self, _key):
        return self.queued


GENERATION_NAME = "20260101T000000"
WORKER_HEALTH = b"Aug-25 11:41:20 j_complete=41 j_failed=1 j_retried=0 j_ongoing=2 queued=3"


def reset_graph_state():
    """Removes the graph symlink, every generation and the build status file."""
    link = SETTINGS.get_graph_link()
    if link.is_symlink():
        link.unlink()
    rmtree(SETTINGS.get_generations_dir(), ignore_errors=True)
    SETTINGS.get_build_status_path().unlink(missing_ok=True)


def make_generation(name=GENERATION_NAME, meta=None):
    """Creates a generation and points the graph symlink at it, the way a finished build would."""
    generation = SETTINGS.get_generations_dir().joinpath(name)
    generation.mkdir(parents=True)
    if meta is not None:
        generation.joinpath("build_meta.json").write_text(json.dumps(meta), encoding="utf8")
    swap_graph_link(SETTINGS.get_graph_link(), generation)

    return generation


def write_build_status(**overrides):
    """Writes a build status report, starting from an idle one."""
    report = {
        "state": "idle",
        "stage": None,
        "generation": None,
        "started_at": None,
        "updated_at": None,
        "next_build_at": None,
        "last_error": None,
    }
    report.update(overrides)
    SETTINGS.get_build_status_path().parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.get_build_status_path().write_text(json.dumps(report), encoding="utf8")

    return report


def create_new_user(client: TestClient, data: dict, auth_header, must_succeed=True) -> Response:
    """
    Helper function for valid new user creation.
    """
    response = client.post("/api/v1/users/", headers=auth_header, json=data)

    if must_succeed:
        res_json = response.json()
        assert (
            response.status_code == 200
        ), f"status code was {response.status_code} with {response.json()}"
        assert response.headers["Content-Type"] == "application/json"
        assert set(res_json.keys()) >= {"id", "email"}
        return response

    return response


def create_new_job(client, data, auth_header, must_succeed=True) -> Response:
    """
    Helper function for valid new job creation.
    """
    response = client.post("/api/v1/jobs/", headers=auth_header, json=data)

    if must_succeed:
        assert (
            response.status_code == 200
        ), f"status code was {response.status_code} with {response.content}"
        assert response.headers["Content-Type"] == "application/json"
        return response
    return response


def create_new_key(client, data, auth_header, must_succeed=True) -> Response: 
    """
    Helper function for new api key creation.
    """
    response = client.post("/api/v1/keys/", headers=auth_header, json=data)

    if must_succeed:
        assert (
            response.status_code == 200
        ), f"status code was {response.status_code} with {response.content}"
        assert response.headers["Content-Type"] == "application/json"
        return response
    return response


def create_package_params(j):
    """
    Create the parameters for create_package task, with user ID 1.

    :param dict j: The job response in JSON

    :returns: Tuple with all parameters inside
    :rtype: tuple
    """
    output_dir = SETTINGS.get_output_path()

    result_path = make_package_path(output_dir, j["name"], j["provider"])

    return {}, j["id"], j["name"], j["description"], j["bbox"], result_path, 1
