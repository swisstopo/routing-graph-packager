from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from routing_packager_app.api_v1.models import APIPermission
from routing_packager_app.config import SETTINGS

from routing_packager_app.utils.file_utils import get_deployed_providers

from tests.utils_ import PROVIDER, make_generation, reset_graph_state

from .conftest import GENERATION_NAME, GRAPH_META, FakePool


def test_fail_without_credentials(get_client: TestClient):
    res = get_client.get("/api/v1/health")

    assert res.status_code == 401


def test_fail_with_a_read_key(get_client: TestClient, create_key_header):
    key = create_key_header(APIPermission.READ, 1)
    res = get_client.get("/api/v1/health", headers={"x-api-key": key["key"]})

    assert res.status_code == 401


def test_success_with_an_internal_key(get_client: TestClient, create_key_header):
    key = create_key_header(APIPermission.INTERNAL, 1)
    res = get_client.get("/api/v1/health", headers={"x-api-key": key["key"]})

    assert res.status_code == 200


def test_success_with_basic_auth(get_client: TestClient, basic_auth_header: dict):
    res = get_client.get("/api/v1/health", headers=basic_auth_header)

    assert res.status_code == 200
    assert set(res.json()) == {"status", "providers", "services"}
    assert set(res.json()["providers"][PROVIDER]) == {"graph", "build"}


def test_reports_no_graph(get_client: TestClient, basic_auth_header: dict):
    res = get_client.get("/api/v1/health", headers=basic_auth_header).json()

    assert res["providers"][PROVIDER]["graph"]["available"] is False
    assert res["status"] == "degraded"


def test_reports_the_current_generation(get_client: TestClient, basic_auth_header: dict, graph):
    res = get_client.get("/api/v1/health", headers=basic_auth_header).json()

    assert res["providers"][PROVIDER]["graph"]["available"] is True
    assert res["providers"][PROVIDER]["graph"]["generation"] == GENERATION_NAME
    assert res["providers"][PROVIDER]["graph"]["valhalla_version"] == "3.8.3"
    assert res["status"] == "ok"


def test_reports_an_unknown_build_without_a_status_file(get_client: TestClient, basic_auth_header: dict):
    res = get_client.get("/api/v1/health", headers=basic_auth_header).json()

    assert res["providers"][PROVIDER]["build"]["state"] == "unknown"


def test_reports_the_next_build(get_client: TestClient, basic_auth_header: dict, write_status):
    write_status(state="idle", next_build_at="2026-09-01T03:00:00+00:00")
    res = get_client.get("/api/v1/health", headers=basic_auth_header).json()

    assert res["providers"][PROVIDER]["build"]["state"] == "idle"
    assert res["providers"][PROVIDER]["build"]["next_build_at"] == "2026-09-01T03:00:00+00:00"


def test_reports_a_running_build(get_client: TestClient, basic_auth_header: dict, write_status):
    write_status(
        state="building",
        stage="building_tiles",
        generation="20260825T113047",
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    res = get_client.get("/api/v1/health", headers=basic_auth_header).json()

    assert res["providers"][PROVIDER]["build"]["stage"] == "building_tiles"
    assert res["providers"][PROVIDER]["build"]["generation"] == "20260825T113047"


def test_reports_a_failed_build(get_client: TestClient, basic_auth_header: dict, write_status):
    write_status(state="failed", stage="updating_pbf", last_error="boom")
    res = get_client.get("/api/v1/health", headers=basic_auth_header).json()

    assert res["providers"][PROVIDER]["build"]["state"] == "failed"
    assert res["providers"][PROVIDER]["build"]["last_error"] == "boom"


def test_reports_postgres_up(get_client: TestClient, basic_auth_header: dict):
    res = get_client.get("/api/v1/health", headers=basic_auth_header).json()

    assert res["services"]["postgres"] == {"up": True, "error": None}


def test_reports_the_worker(get_client: TestClient, basic_auth_header: dict):
    worker = get_client.get("/api/v1/health", headers=basic_auth_header).json()["services"]["worker"]

    assert worker["up"] is True
    assert worker["last_report"] == "Aug-25 11:41:20"
    assert worker["queued"] == 3
    assert worker["ongoing"] == 2
    assert worker["complete"] == 41
    assert worker["failed"] == 1


def test_reports_a_dead_worker(get_client: TestClient, basic_auth_header: dict, get_app):
    get_app.state.redis_pool = FakePool(health=None, queued=4)
    res = get_client.get("/api/v1/health", headers=basic_auth_header).json()

    assert res["services"]["redis"]["up"] is True
    assert res["services"]["worker"]["up"] is False
    assert res["services"]["worker"]["queued"] == 4
    assert res["status"] == "degraded"


def test_reports_redis_down(get_client: TestClient, basic_auth_header: dict, get_app):
    get_app.state.redis_pool = FakePool(fails=True)
    res = get_client.get("/api/v1/health", headers=basic_auth_header).json()

    assert res["services"]["redis"]["up"] is False
    assert "unreachable" in res["services"]["redis"]["error"]
    assert res["services"]["worker"]["up"] is False


def test_reports_redis_down_without_a_pool(get_client: TestClient, basic_auth_header: dict, get_app):
    get_app.state.redis_pool = None
    res = get_client.get("/api/v1/health", headers=basic_auth_header).json()

    assert res["services"]["redis"]["up"] is False
    assert res["services"]["postgres"]["up"] is True


def test_answers_with_the_admin_credentials_while_postgres_is_down(
    get_client: TestClient, basic_auth_header: dict, monkeypatch
):
    def explode(*args, **kwargs):
        raise ConnectionError("could not connect to server")

    monkeypatch.setattr("routing_packager_app.api_v1.routes.health.Session.exec", explode)
    monkeypatch.setattr("routing_packager_app.api_v1.routes.health.APIKeys.check_key", explode)
    res = get_client.get("/api/v1/health", headers=basic_auth_header)

    assert res.status_code == 200
    assert res.json()["services"]["postgres"]["up"] is False
    assert "could not connect" in res.json()["services"]["postgres"]["error"]
    assert res.json()["status"] == "degraded"


def test_admin_credentials_must_match(get_client: TestClient):
    from base64 import b64encode

    wrong = b64encode(f"{SETTINGS.ADMIN_EMAIL}:not-the-password".encode()).decode()
    res = get_client.get("/api/v1/health", headers={"Authorization": f"Basic {wrong}"})

    assert res.status_code == 401


@pytest.fixture
def other_provider():
    """A provider other than the one the single-provider tests use, cleaned up after."""
    provider = next(p for p in get_deployed_providers() if p != PROVIDER)
    yield provider
    reset_graph_state(provider)


def test_reports_every_deployed_provider(get_client: TestClient, basic_auth_header: dict):
    res = get_client.get("/api/v1/health", headers=basic_auth_header).json()

    assert set(res["providers"]) == set(get_deployed_providers())


def test_each_provider_reports_its_own_graph(
    get_client: TestClient, basic_auth_header: dict, graph, other_provider
):
    res = get_client.get("/api/v1/health", headers=basic_auth_header).json()

    assert res["providers"][PROVIDER]["graph"]["available"] is True
    # deployed, but its first build has not landed
    assert res["providers"][other_provider]["graph"]["available"] is False
    assert other_provider in res["providers"][other_provider]["graph"]["path"]


def test_one_available_graph_is_enough_to_be_healthy(
    get_client: TestClient, basic_auth_header: dict, graph
):
    res = get_client.get("/api/v1/health", headers=basic_auth_header).json()

    assert res["status"] == "ok"


def test_a_graph_from_any_provider_makes_it_healthy(
    get_client: TestClient, basic_auth_header: dict, other_provider
):
    make_generation(meta=GRAPH_META, provider=other_provider)

    res = get_client.get("/api/v1/health", headers=basic_auth_header).json()

    assert res["status"] == "ok"
    assert res["providers"][PROVIDER]["graph"]["available"] is False


def test_each_provider_reports_its_own_build(
    get_client: TestClient, basic_auth_header: dict, write_status, other_provider
):
    write_status(provider=other_provider, state="building", stage="building_tiles")

    res = get_client.get("/api/v1/health", headers=basic_auth_header).json()

    assert res["providers"][other_provider]["build"]["stage"] == "building_tiles"
    assert res["providers"][PROVIDER]["build"]["state"] == "unknown"
