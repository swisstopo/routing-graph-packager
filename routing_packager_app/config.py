import os
import sys
from pathlib import Path
from typing import List

from pydantic import model_validator
from pydantic_settings import BaseSettings as _BaseSettings
from pydantic_settings import SettingsConfigDict

BASE_DIR = Path(__file__).parent.parent.resolve()
ENV_FILE = BASE_DIR.joinpath(".env")


class BaseSettings(_BaseSettings):
    SECRET_KEY: str = "<MMs8?u_;rTt>;LarIGI&FjWhKNSe=%3|W;=DFDqOdx+~-rBS+K=p8#t#9E+;{e$"
    SQLALCHEMY_TRACK_MODIFICATIONS: bool = False

    DESCRIPTION_PATH: Path = BASE_DIR.joinpath("DESCRIPTION.md")

    # APP ###
    ADMIN_EMAIL: str = "admin@example.org"
    ADMIN_PASS: str = "admin"
    # TODO: clarify if there's a need to restrict origins
    CORS_ORIGINS: List[str] = ["*"]

    DATA_DIR: Path = BASE_DIR.joinpath("data")
    TMP_DATA_DIR: Path = BASE_DIR.joinpath("tmp_data")

    # GRAPH BUILD ###
    GRAPH_BUILD_CRON: str = "0 3 * * 0"  # 03:00 AM every Sunday
    GRAPH_PRUNE_TIMEOUT: int = 3600
    GRAPH_LOCK_TTL: int = 345600
    PBF_LOCAL_PATH: Path | None = None
    PBF_URL: str = "https://planet.openstreetmap.org/pbf/planet-latest.osm.pbf"
    PBF_FORCE_UPDATE: bool = False
    PBF_MAX_UPDATE_PASSES: int = 10
    PBF_UPDATE_SIZE_MB: int = 1024
    USE_ELEVATION: bool = False
    CONCURRENCY: int = 8
    MAX_CACHE_SIZE: int = 1000000000

    # DATABASES ###
    POSTGRES_HOST: str = "postgis"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "gis"
    POSTGRES_USER: str = "docker"
    POSTGRES_PASS: str = "docker"
    REDIS_URL: str = "redis://localhost"

    # MONITORING ###
    METRICS_PORT: int = 9101
    STATSD_HOST: str = ""
    STATSD_PORT: int = 8125
    STATSD_PREFIX: str = "rgp"

    # SMTP ###
    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 1025
    SMTP_FROM: str = "valhalla@kadas.org"
    SMTP_USER: str = ""
    SMTP_PASS: str = ""
    SMTP_SECURE: bool = False

    model_config = SettingsConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _ignore_empty_env_vars(cls, data):
        """
        Falls back to the defaults for env vars that are set but empty.
        """
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v != ""}

        return data

    def get_provider_dir(self, provider: str) -> Path:
        """
        Return the root directory holding one provider's graph generations.
        """
        return self.get_tmp_data_dir().joinpath(provider)

    def get_graph_link(self, provider: str) -> Path:
        """
        Return the symlink pointing at the generation currently served to packaging jobs.
        """
        return self.get_provider_dir(provider).joinpath("graph")

    def get_generations_dir(self, provider: str) -> Path:
        """
        Return the directory holding every built graph generation for a provider.
        """
        return self.get_provider_dir(provider).joinpath("generations")

    def get_build_status_path(self, provider: str) -> Path:
        """
        Return the file the graph builder publishes its current stage to. It's
        a small JSON file that the graph builder continuously updates, which
        serves as a way to let the API report on the builder's status.
        """
        return self.get_provider_dir(provider).joinpath("build_status.json")

    def get_pbf_path(self, provider: str) -> Path:
        """
        Return the local OSM PBF a provider's graph is built from.

        :param provider: the dataset provider whose PBF to locate.
        """
        if self.PBF_LOCAL_PATH is not None:
            return Path(self.PBF_LOCAL_PATH)

        return self.get_provider_dir(provider).joinpath("planet-latest.osm.pbf")

    def get_elevation_dir(self) -> Path:
        """
        Return the elevation tile directory, shared across all graph generations.
        """
        return self.get_tmp_data_dir().joinpath("elevation")

    def get_output_path(self) -> Path:
        return self.get_data_dir().joinpath("output")

    def get_data_dir(self) -> Path:
        data_dir = self.DATA_DIR
        # if we're inside a docker container, we need to reference the fixed directory instead
        # Watch out for CI, also runs within docker
        if os.path.isdir("/app") and not os.getenv("CI", None):  # pragma: no cover
            data_dir = Path("/app/data")

        return data_dir

    def get_tmp_data_dir(self) -> Path:
        tmp_data_dir = self.TMP_DATA_DIR
        # if we're inside a docker container, we need to reference the fixed directory instead
        # Watch out for CI, also runs within docker
        if os.path.isdir("/app") and not os.getenv("CI", None):  # pragma: no cover
            tmp_data_dir = Path("/app/tmp_data")

        return tmp_data_dir

    def get_logging_dir(self) -> Path:
        """
        Gets the path where logs are stored for the worker, the graph builder and the app
        """
        tmp_data_dir = self.TMP_DATA_DIR
        if os.path.isdir("/app") and not os.getenv("CI", None):  # pragma: no cover
            tmp_data_dir = Path("/app/tmp_data")
        log_dir = tmp_data_dir / "logs"

        log_dir.mkdir(exist_ok=True, parents=True)

        return log_dir


class ProdSettings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=True, env_file=ENV_FILE, extra="ignore")


class DevSettings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=True, env_file=ENV_FILE, extra="ignore")


class TestSettings(BaseSettings):
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "gis_test"
    POSTGRES_DB_TEST: str = "gis_test"
    POSTGRES_USER: str = "admin"
    POSTGRES_PASS: str = "admin"

    DATA_DIR: Path = BASE_DIR.joinpath("tests", "data")
    TMP_DATA_DIR: Path = BASE_DIR.joinpath("tests", "tmp_data")

    ADMIN_EMAIL: str = "admin@example.org"
    ADMIN_PASS: str = "admin"
    model_config = SettingsConfigDict(
        case_sensitive=True, env_file=BASE_DIR.joinpath("tests", "env"), extra="ignore"
    )


# decide which settings we'll use
SETTINGS: BaseSettings
env = os.getenv("API_CONFIG", "prod")
if env == "prod":  # pragma: no cover
    SETTINGS = ProdSettings()
elif env == "dev":  # pragma: no cover
    SETTINGS = DevSettings()
elif env == "test":
    SETTINGS = TestSettings()
else:  # pragma: no cover
    print("No valid 'API_CONFIG' environment variable, one of 'prod', 'dev' or 'test'", file=sys.stderr)
    sys.exit(1)
