import pytest

from routing_packager_app.config import SETTINGS
from routing_packager_app.constants import PROVIDERS
from routing_packager_app.utils.file_utils import get_deployed_providers


@pytest.fixture
def tmp_volume(tmp_path, monkeypatch):
    """An empty shared volume, as it looks before any graph build container has started."""
    monkeypatch.setattr(SETTINGS, "TMP_DATA_DIR", tmp_path)

    return tmp_path


def test_no_provider_is_deployed_before_a_builder_ran(tmp_volume):
    assert get_deployed_providers() == []


def test_a_provider_directory_is_what_marks_it_deployed(tmp_volume):
    tmp_volume.joinpath("tomtom").mkdir()

    assert get_deployed_providers() == ["tomtom"]


def test_every_deployed_provider_is_reported_in_declaration_order(tmp_volume):
    for provider in reversed(PROVIDERS):
        tmp_volume.joinpath(provider).mkdir()

    assert get_deployed_providers() == PROVIDERS


def test_an_unknown_directory_is_not_a_provider(tmp_volume):
    tmp_volume.joinpath("osm").mkdir()
    tmp_volume.joinpath("elevation").mkdir()
    tmp_volume.joinpath("logs").mkdir()

    assert get_deployed_providers() == ["osm"]


def test_a_file_named_after_a_provider_does_not_count(tmp_volume):
    tmp_volume.joinpath("osm").write_text("not a directory", encoding="utf8")

    assert get_deployed_providers() == []


def test_each_provider_builds_from_its_own_pbf():
    """Two builders sharing one PBF would rewrite it under each other, see update_pbf."""
    assert SETTINGS.get_pbf_path("osm") != SETTINGS.get_pbf_path("tomtom")
    assert SETTINGS.get_pbf_path("osm").parent == SETTINGS.get_provider_dir("osm")


def test_an_explicit_pbf_path_wins(tmp_path, monkeypatch):
    pbf = tmp_path.joinpath("switzerland-latest.osm.pbf")
    monkeypatch.setattr(SETTINGS, "PBF_LOCAL_PATH", pbf)

    assert SETTINGS.get_pbf_path("osm") == pbf
