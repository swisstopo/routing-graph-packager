import os
from shutil import rmtree

import pytest
from fastapi.testclient import TestClient

from routing_packager_app.config import SETTINGS
from tests.utils_ import FakePool, write_build_status

URL = "/api/v1/readyz"


def test_ready_when_everything_is_up(get_client: TestClient, graph):
    res = get_client.get(URL)

    assert res.status_code == 200
    assert res.json() == {"ready": True}


def test_needs_no_credentials(get_client: TestClient, graph):
    res = get_client.get(URL, headers={})

    assert res.status_code == 200


def test_not_ready_without_a_graph(get_client: TestClient):
    res = get_client.get(URL)

    assert res.status_code == 503
    assert res.json() == {"ready": False}


def test_not_ready_when_the_graph_link_dangles(get_client: TestClient, graph):
    rmtree(graph)

    assert get_client.get(URL).status_code == 503


def test_not_ready_without_a_redis_pool(get_client: TestClient, get_app, graph):
    get_app.state.redis_pool = None

    assert get_client.get(URL).status_code == 503


def test_not_ready_when_redis_is_unreachable(get_client: TestClient, get_app, graph):
    get_app.state.redis_pool = FakePool(fails=True)

    assert get_client.get(URL).status_code == 503


def test_not_ready_when_postgres_is_down(get_client: TestClient, graph, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("could not connect to server")

    monkeypatch.setattr("routing_packager_app.api_v1.routes.readiness.Session.execute", explode)

    assert get_client.get(URL).status_code == 503


def test_not_ready_without_an_output_directory(get_client: TestClient, graph, tmp_path, monkeypatch):
    monkeypatch.setattr(SETTINGS, "DATA_DIR", tmp_path)

    assert get_client.get(URL).status_code == 503


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes regardless of the mode bits")
def test_not_ready_when_the_output_directory_is_read_only(
    get_client: TestClient, graph, tmp_path, monkeypatch
):
    output = tmp_path.joinpath("output")
    output.mkdir()
    output.chmod(0o500)
    monkeypatch.setattr(SETTINGS, "DATA_DIR", tmp_path)
    try:
        assert get_client.get(URL).status_code == 503
    finally:
        output.chmod(0o700)


def test_a_running_build_stays_ready(get_client: TestClient, graph):
    write_build_status(state="building", stage="building_tiles", generation="20260201T000000")

    assert get_client.get(URL).status_code == 200


def test_a_dead_worker_stays_ready(get_client: TestClient, get_app, graph):
    get_app.state.redis_pool = FakePool(health=None, queued=4)

    assert get_client.get(URL).status_code == 200


def test_the_path_carries_no_trailing_slash(get_client: TestClient, graph):
    assert get_client.get(URL, follow_redirects=False).status_code == 200
    assert get_client.get(URL + "/", follow_redirects=False).status_code != 200
