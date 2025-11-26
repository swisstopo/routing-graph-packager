import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from arq.connections import RedisSettings
from fastapi import HTTPException
import requests
from requests.exceptions import ConnectionError
import shutil
from sqlmodel import Session, select
from starlette.status import (
    HTTP_200_OK,
    HTTP_404_NOT_FOUND,
    HTTP_500_INTERNAL_SERVER_ERROR,
    HTTP_301_MOVED_PERMANENTLY,
)

from .api_v1.dependencies import split_bbox
from .config import SETTINGS
from .db import get_db
from .api_v1.models import User, Job
from .constants import Statuses
from .logger import AppSmtpHandler, get_smtp_details, LOGGER
from .utils.file_utils import make_zip
from .utils.valhalla_utils import get_tiles_with_bbox


async def create_package(
    ctx,
    job_id: int,
    job_name: str,
    description: str,
    bbox: str,
    zip_path: str,
    user_id: int | None,
    update: bool = False,
):
    session: Session = next(get_db())

    # Set up the logger where we have access to the user email
    # and only if there hasn't been one before
    if user_id is not None:
        statement = select(User).where(User.id == user_id)
        results = session.exec(statement).first()

        if results is None:
            raise HTTPException(
                HTTP_404_NOT_FOUND,
                "No user with specified ID found.",
            )
        user_email = results.email
        if not LOGGER.handlers and update is False:
            handler = AppSmtpHandler(**get_smtp_details([user_email]))
            handler.setLevel(logging.INFO)
            LOGGER.addHandler(handler)
    else:
        user_email = ""
    log_extra = {"user": user_email, "job_id": job_id}

    statement = select(Job).where(Job.id == job_id)
    job = session.exec(statement).first()
    if job is None:
        raise HTTPException(
            HTTP_404_NOT_FOUND,
            "No job with specified ID found.",
        )
    job.status = Statuses.COMPRESSING
    job.last_started = datetime.now(timezone.utc)
    session.commit()

    succeeded = False
    try:
        # TODO: gzipping is synchronous, maybe follow
        #   https://arq-docs.helpmanual.io/#synchronous-jobs

        # get the active Valhalla instance
        current_valhalla_dir_str = ""
        for port in (8002, 8003):
            try:
                status = requests.get(f"{SETTINGS.VALHALLA_URL}:{port}/status").status_code
                LOGGER.info(f"checking {SETTINGS.VALHALLA_URL}:{port}/status", extra=log_extra)
                # 301 is what the test "expects" due to the simple HTTP server
                if status not in (HTTP_200_OK, HTTP_301_MOVED_PERMANENTLY):
                    continue
                current_valhalla_dir_str = SETTINGS.get_valhalla_path(port)
                break
            except ConnectionError:
                pass

        if not current_valhalla_dir_str:
            raise HTTPException(
                HTTP_500_INTERNAL_SERVER_ERROR,
                "No Valhalla service online, check the Valhalla server's docker logs.",
            )

        current_valhalla_dir = Path(current_valhalla_dir_str).resolve()
        valhalla_tiles = sorted(current_valhalla_dir.rglob("*.gph"))
        if not valhalla_tiles or not current_valhalla_dir_str:
            raise HTTPException(404, f"No Valhalla tiles in {current_valhalla_dir.resolve()}")

        # Gather Valhalla tile paths
        tile_paths = get_tiles_with_bbox(valhalla_tiles, split_bbox(bbox), current_valhalla_dir)
        if not tile_paths:
            raise HTTPException(404, f"No Valhalla tiles in bbox {bbox}")

        # zip up the tiles after locking the directory to not be updated right now
        out_dir = SETTINGS.get_output_path()
        lock = out_dir.joinpath(".lock")
        lock.touch(exist_ok=False)
        try:
            make_zip(tile_paths, current_valhalla_dir, zip_path)
        except Exception as e:
            LOGGER.error(e)
        finally:
            lock.unlink(missing_ok=False)

        # Create the meta JSON
        fname = os.path.basename(zip_path)
        j = {
            "job_id": job_id,
            "filepath": fname,
            "name": job_name,
            "description": description,
            "extent": bbox,
            "last_modified": str(datetime.now(timezone.utc)),
        }
        dirname = os.path.dirname(zip_path)
        fname_sanitized = fname.split(os.extsep, 1)[0]
        with open(os.path.join(dirname, fname_sanitized + ".json"), "w", encoding="utf8") as f:
            json.dump(j, f, indent=2, ensure_ascii=False)

        LOGGER.info(
            f"Job {job_id} by {user_email} finished successfully. Find the new dataset in {zip_path}",
            extra=log_extra,
        )
        succeeded = True
    # catch all exceptions we're controlling
    except HTTPException as e:
        LOGGER.critical(f"Job {job.name} failed with\n'{e.detail}'", extra=log_extra)
        raise e
    # any other exception is assumed to be a deleted job and will only be logged/email sent
    except Exception:  # pragma: no cover
        msg = f"Job {job.name} by {user_email} was deleted."
        LOGGER.critical(msg, extra=log_extra)
        raise
    finally:
        final_status = Statuses.COMPLETED
        if not succeeded:
            shutil.rmtree(os.path.dirname(zip_path))
            final_status = Statuses.FAILED

        # always write the "last_finished" column
        job.last_finished = datetime.now(timezone.utc)
        job.status = final_status
        session.commit()


class WorkerSettings:
    """
    Settings for the ARQ worker.
    """

    redis_settings = RedisSettings.from_dsn(SETTINGS.REDIS_URL)
    functions = [create_package]
