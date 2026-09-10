from shutil import rmtree, copytree

import pytest
from sqlmodel import Session, select

from routing_packager_app import SETTINGS
from routing_packager_app.api_v1.models import Job
from routing_packager_app.graph_build.builder import swap_graph_link
from tests.utils_ import PROVIDER

GENERATION_NAME = "20260101T000000"


def _reset_graph():
    link = SETTINGS.get_graph_link(PROVIDER)
    if link.is_symlink():
        link.unlink()
    rmtree(SETTINGS.get_generations_dir(PROVIDER), ignore_errors=True)


@pytest.fixture(scope="function", autouse=True)
def clean_graph():
    _reset_graph()
    yield
    _reset_graph()


@pytest.fixture(scope="function", autouse=True)
def delete_jobs(get_session: Session):
    yield
    jobs = get_session.exec(select(Job)).all()
    for job in jobs:
        get_session.delete(job)
        get_session.commit()


@pytest.fixture(scope="function", autouse=True)
def delete_dirs():
    yield
    try:
        for dir_ in SETTINGS.get_output_path().iterdir():
            rmtree(dir_)
    except Exception:  # failing tests may not have produced any output
        pass


@pytest.fixture(scope="function")
def empty_graph():
    generation = SETTINGS.get_generations_dir(PROVIDER).joinpath(GENERATION_NAME)
    generation.mkdir(parents=True)
    swap_graph_link(SETTINGS.get_graph_link(PROVIDER), generation)

    yield generation


@pytest.fixture(scope="function")
def copy_valhalla_tiles(empty_graph):
    for dir_ in SETTINGS.get_output_path().parent.joinpath("andorra_tiles").iterdir():
        copytree(dir_, empty_graph.joinpath(dir_.stem))

    yield empty_graph
