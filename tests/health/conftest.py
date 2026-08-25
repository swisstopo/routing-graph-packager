import json
from shutil import rmtree

import pytest

from routing_packager_app import SETTINGS
from routing_packager_app.graph_build.builder import swap_graph_link
from routing_packager_app.utils.file_utils import create_lock_file

GENERATION_NAME = "20260101T000000"
WORKER_HEALTH = b"Aug-25 11:41:20 j_complete=41 j_failed=1 j_retried=0 j_ongoing=2 queued=3"


class FakePool:
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


def _reset():
    link = SETTINGS.get_graph_link()
    if link.is_symlink():
        link.unlink()
    rmtree(SETTINGS.get_generations_dir(), ignore_errors=True)
    SETTINGS.get_build_status_path().unlink(missing_ok=True)


@pytest.fixture(scope="function", autouse=True)
def clean_state(get_app):
    _reset()
    get_app.state.redis_pool = FakePool(health=WORKER_HEALTH, queued=3)
    yield
    _reset()
    get_app.state.redis_pool = None


@pytest.fixture(scope="function")
def graph():
    generation = SETTINGS.get_generations_dir().joinpath(GENERATION_NAME)
    generation.mkdir(parents=True)
    create_lock_file(generation)
    generation.joinpath("build_meta.json").write_text(
        json.dumps({"generation": GENERATION_NAME, "valhalla_version": "3.8.3", "elevation": False}),
        encoding="utf8",
    )
    swap_graph_link(SETTINGS.get_graph_link(), generation)

    yield generation


@pytest.fixture(scope="function")
def write_status():
    def write(**overrides):
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

    return write
