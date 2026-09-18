import pytest

from tests.utils_ import FakePool, WORKER_HEALTH, make_generation, reset_graph_state


@pytest.fixture(scope="function", autouse=True)
def clean_state(get_app):
    reset_graph_state()
    get_app.state.redis_pool = FakePool(health=WORKER_HEALTH, queued=3)
    yield
    reset_graph_state()
    get_app.state.redis_pool = None


@pytest.fixture(scope="function")
def graph():
    yield make_generation()
