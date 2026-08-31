import pytest

from tests.utils_ import (  # noqa: F401
    GENERATION_NAME,
    WORKER_HEALTH,
    FakePool,
    make_generation,
    reset_graph_state,
    write_build_status,
)

GRAPH_META = {"generation": GENERATION_NAME, "valhalla_version": "3.8.3", "elevation": False}


@pytest.fixture(scope="function", autouse=True)
def clean_state(get_app):
    reset_graph_state()
    get_app.state.redis_pool = FakePool(health=WORKER_HEALTH, queued=3)
    yield
    reset_graph_state()
    get_app.state.redis_pool = None


@pytest.fixture(scope="function")
def graph():
    yield make_generation(meta=GRAPH_META)


@pytest.fixture(scope="function")
def write_status():
    return write_build_status
