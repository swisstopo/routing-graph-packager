"""
Publishes what the graph builder is currently doing to a file on the shared volume.

The builder runs in its own container, so the API app cannot ask it anything directly. Instead
every stage transition is written atomically to ``build_status.json`` next to the ``graph``
symlink, where ``/api/v1/health`` reads it.

Long stages additionally refresh ``updated_at`` as a heartbeat, which is what lets a reader tell
a build that is still working from one whose container was killed mid-run.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from ..config import SETTINGS
from ..constants import BuildStage, BuildState

HEARTBEAT_INTERVAL = 10.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BuildStatus:
    def __init__(self, path: Path):
        self.path = path
        self._last_heartbeat = 0.0
        self._data = {
            "state": BuildState.UNKNOWN.value,
            "stage": None,
            "generation": None,
            "started_at": None,
            "updated_at": None,
            "next_build_at": None,
            "last_error": None,
        }

    def stage(self, stage: BuildStage) -> None:
        """
        Records that the build moved on to ``stage``.

        :param stage: the step the builder is about to start.
        """
        if self._data["state"] != BuildState.BUILDING.value:
            self._data["started_at"] = _now()
            self._data["last_error"] = None
            self._data["generation"] = None

        self._data["state"] = BuildState.BUILDING.value
        self._data["stage"] = stage.value
        self._data["next_build_at"] = None
        self._write()

    def generation(self, generation: str) -> None:
        """
        Records which generation directory the running build writes into.

        :param generation: the generation's directory name.
        """
        self._data["generation"] = generation
        self._write()

    def heartbeat(self) -> None:
        """
        Refreshes ``updated_at`` if the throttling interval has passed.

        Called for every line a build subprocess emits, so it has to stay cheap: the common case
        is a single monotonic clock comparison.
        """
        now = time.monotonic()
        if now - self._last_heartbeat < HEARTBEAT_INTERVAL:
            return
        self._write()

    def idle(self, next_build_at: datetime | None = None) -> None:
        """
        Records that no build is running.

        :param next_build_at: when the next cron occurrence is due, if it is known.
        """
        self._data["state"] = BuildState.IDLE.value
        self._data["stage"] = None
        self._data["next_build_at"] = next_build_at.isoformat() if next_build_at else None
        self._write()

    def failed(self, error: str) -> None:
        """
        Records that the build gave up, keeping the stage it died on.

        :param error: the message to surface to an operator.
        """
        self._data["state"] = BuildState.FAILED.value
        self._data["last_error"] = error
        self._write()

    def _write(self) -> None:
        self._last_heartbeat = time.monotonic()
        self._data["updated_at"] = _now()

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            staged = self.path.with_name(self.path.name + ".tmp")
            staged.write_text(json.dumps(self._data, indent=2), encoding="utf8")
            os.replace(staged, self.path)
        except OSError:
            pass


BUILD_STATUS = BuildStatus(SETTINGS.get_build_status_path())
