import os
import sys
from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings as _BaseSettings
from pydantic_settings import SettingsConfigDict
from starlette.datastructures import CommaSeparatedStrings

from routing_packager_app.constants import Providers

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
    VALHALLA_URL: str = "http://localhost"

    ENABLED_PROVIDERS: list[str] = list(CommaSeparatedStrings("osm"))

    # DATABASES ###
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "gis"
    POSTGRES_USER: str = "docker"
    POSTGRES_PASS: str = "docker"
    REDIS_URL: str = "redis://localhost"

    # SMTP ###
    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 1025
    SMTP_FROM: str = "valhalla@kadas.org"
    SMTP_USER: str = ""
    SMTP_PASS: str = ""
    SMTP_SECURE: bool = False

    model_config = SettingsConfigDict(extra="ignore")

    def get_valhalla_path(self, port: int) -> Path:  # pragma: no cover
        """
        Return the path to the OSM Valhalla instances.
        """
        if port in (8002, 8003):
            p = self.get_tmp_data_dir().joinpath(Providers.OSM.lower(), str(port))
            p.mkdir(exist_ok=True, parents=True)
            return p
        raise ValueError(f"{port} is not a valid port for Valhalla.")

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
        Gets the path where logs are stored for both worker and builder/app
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
