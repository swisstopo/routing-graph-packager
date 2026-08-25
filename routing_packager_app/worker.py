import json
import logging
import os
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from typing import List, Sequence

from arq.connections import RedisSettings
from fastapi import HTTPException
import shutil
from sqlmodel import Session, select
from starlette.status import (
    HTTP_404_NOT_FOUND,
    HTTP_500_INTERNAL_SERVER_ERROR,
)

from .api_v1.dependencies import split_bbox
from .config import SETTINGS
from .db import get_db
from .api_v1.models import User, Job
from .constants import Statuses
from .logger import AppSmtpHandler, get_smtp_details, LOGGER
from .utils.file_utils import lock_generation_shared, make_zip
from .utils.geom_utils import wkbe_to_geom, wkbe_to_str
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

        graph_link = SETTINGS.get_graph_link()
        stack = ExitStack()
        try:
            current_valhalla_dir = stack.enter_context(lock_generation_shared(graph_link))
        except OSError as e:
            raise HTTPException(
                HTTP_500_INTERNAL_SERVER_ERROR,
                f"No graph available behind {graph_link}, check the graph build container's logs ({e}).",
            )

        with stack:
            LOGGER.info(f"Packaging from graph generation {current_valhalla_dir.name}", extra=log_extra)
            valhalla_tiles = sorted(current_valhalla_dir.rglob("*.gph"))
            if not valhalla_tiles:
                raise HTTPException(HTTP_404_NOT_FOUND, f"No Valhalla tiles in {current_valhalla_dir}")

            tile_paths = get_tiles_with_bbox(valhalla_tiles, split_bbox(bbox), current_valhalla_dir)
            if not tile_paths:
                raise HTTPException(HTTP_404_NOT_FOUND, f"No Valhalla tiles in bbox {bbox}")

            make_zip(tile_paths, current_valhalla_dir, zip_path)

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


def _sort_jobs(jobs_: Sequence[Job]) -> List[Job]:
    """
    Sorts jobs by bbox area, largest first.

    :param jobs_: the jobs to sort.

    :returns: the sorted jobs.
    """
    return [
        job
        for _, job in sorted(
            ((wkbe_to_geom(job.bbox).area, job) for job in jobs_),
            key=lambda x: x[0],
            reverse=True,
        )
    ]


async def update_all_packages(ctx):
    """
    Re-creates every package flagged for updating from the current graph generation.

    Enqueued by the graph build container after it swapped in a new generation. Packages are
    rebuilt in place, sequentially, largest bbox first.
    """
    session: Session = next(get_db())

    admin = session.exec(select(User).where(User.email == SETTINGS.ADMIN_EMAIL)).first()
    user_email = admin.email if admin is not None else ""
    if not LOGGER.handlers and user_email:
        handler = AppSmtpHandler(**get_smtp_details([user_email]))
        handler.setLevel(logging.INFO)
        LOGGER.addHandler(handler)

    jobs = _sort_jobs(session.exec(select(Job).where(Job.update == True)).all())  # noqa: E712
    LOGGER.info(f"Updating {len(jobs)} packages as {user_email}.")

    start_time = time.time()
    succeeded = 0
    for job in jobs:
        try:
            await create_package(
                ctx,
                job.id,
                job.arq_id,
                job.description,
                wkbe_to_str(job.bbox),
                job.zip_path,
                job.user_id,
                True,
            )
            succeeded += 1
        except Exception as e:
            LOGGER.critical(
                f"Updating job {job.name} failed with '{e}'",
                extra={"user": user_email, "job_id": job.id},
            )

    total_time = (time.time() - start_time) / 60
    if succeeded == len(jobs):
        LOGGER.info(f"Updated {succeeded} packages in {total_time:.1f} minutes.")
    else:
        LOGGER.warning(f"Updated {succeeded} of {len(jobs)} packages in {total_time:.1f} minutes.")

    return {"total": len(jobs), "succeeded": succeeded}


class WorkerSettings:
    """
    Settings for the ARQ worker.
    """

    redis_settings = RedisSettings.from_dsn(SETTINGS.REDIS_URL)
    functions = [create_package, update_all_packages]
    job_timeout = 60 * 60 * 24
