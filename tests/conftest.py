import shutil
from base64 import b64encode
from contextlib import asynccontextmanager

import pytest
from arq import Worker
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel

from routing_packager_app import create_app
from routing_packager_app.api_v1.models import APIPermission
from routing_packager_app.config import SETTINGS
from routing_packager_app.worker import create_package, update_all_packages


@asynccontextmanager
async def lifespan(app: FastAPI):
    pass


@pytest.fixture(scope="session", autouse=True)
def create_worker():
    """Auto-runs and creates an event loop for the worker."""
    worker = Worker([create_package, update_all_packages])
    worker.async_run()
    yield
    worker.close()


@pytest.fixture(scope="session", autouse=False)
def get_app() -> FastAPI:
    app = create_app(lifespan=lifespan)

    return app


@pytest.fixture(scope="session")
def get_client(get_app: FastAPI) -> TestClient:
    client = TestClient(get_app)

    return client


@pytest.fixture(scope="session", autouse=True)
def create_db():
    from routing_packager_app.api_v1.models import User
    from routing_packager_app.db import engine

    SQLModel.metadata.create_all(engine, checkfirst=True)
    User.add_admin_user(Session(engine))
    yield
    SQLModel.metadata.drop_all(engine, checkfirst=True)


@pytest.fixture(scope="function")
def get_session():
    from routing_packager_app.db import engine

    db = Session(engine, autocommit=False, autoflush=False)
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(scope="session")
def basic_auth_header():
    admin_email = SETTINGS.ADMIN_EMAIL
    admin_pass = SETTINGS.ADMIN_PASS

    auth_encoded = b64encode(bytes(":".join((admin_email, admin_pass)).encode("utf-8"))).decode()
    auth_header = {"Authorization": f"Basic {auth_encoded}"}

    return auth_header


@pytest.fixture(scope="function")
def create_key_header(get_client: TestClient, basic_auth_header: dict):
    def create_key(permission: APIPermission, validity_days: int):
        res = get_client.post(
            "/api/v1/keys/",
            headers=basic_auth_header,
            json={"permission": permission, "validity_days": validity_days},
        )
        return res.json()

    yield create_key
    try:
        all_keys = get_client.get("/api/v1/keys/", headers=basic_auth_header).json()
        for db_key in all_keys:
            res = get_client.delete(f"/api/v1/keys/{db_key['id']}", headers=basic_auth_header)
            assert res.status_code == 204
    except Exception as e:
        raise RuntimeError(f"Error while deleting test api keys: {e}")


# Creates needed directories and removes them after the test function
@pytest.fixture(scope="session", autouse=True)
def handle_dirs():
    paths = [SETTINGS.get_generations_dir(), SETTINGS.get_logging_dir()]
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)
    yield
    for p in paths:
        shutil.rmtree(p, ignore_errors=True)
