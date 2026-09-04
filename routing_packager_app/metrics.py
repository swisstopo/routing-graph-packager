"""
Monitoring metrics for the app, the worker and the graph build.

Prometheus is used for the app and the worker (long lived services), while StatsD is used for the
graph builder (which may be an externally controlled one-shot).
"""

import json
import os
import time
from datetime import datetime
from typing import Any, Dict, Iterator

import redis
from arq.constants import default_queue_name, health_check_key_suffix, in_progress_key_prefix
from datadog.dogstatsd.base import DogStatsd
from prometheus_client import Counter, Histogram, disable_created_metrics, start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.registry import REGISTRY
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import SETTINGS
from .constants import BuildStage, BuildState

WORKER_HEALTH_KEY = default_queue_name + health_check_key_suffix
IN_PROGRESS_PATTERN = in_progress_key_prefix + "*"

disable_created_metrics()

HTTP_REQUESTS = Counter(
    "rgp_http_requests",
    "HTTP requests served, by method, endpoint and response status.",
    ["method", "endpoint", "status"],
)

HTTP_DURATION = Histogram(
    "rgp_http_duration_seconds",
    "How long the app took to answer a request.",
    ["method", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

PACKAGES = Counter(
    "rgp_package",
    "Packaging jobs that finished, by outcome and whether they re-created an existing package.",
    ["outcome", "update"],
)

PACKAGE_DURATION = Histogram(
    "rgp_package_duration_seconds",
    "How long it took to zip a package out of the current graph generation.",
    ["update"],
    buckets=(1, 5, 15, 30, 60, 300, 900, 1800, 3600, 7200),
)

STATSD_ENABLED = bool(SETTINGS.STATSD_HOST)

# env var read by datadog statsd client
if not STATSD_ENABLED:
    os.environ["DD_DOGSTATSD_DISABLE"] = "true"

STATSD = DogStatsd(
    host=SETTINGS.STATSD_HOST or "localhost",
    port=SETTINGS.STATSD_PORT,
    namespace=SETTINGS.STATSD_PREFIX,
    use_ms=True,
    disable_telemetry=True,
    origin_detection_enabled=False,
)


def parse_worker_health(raw: bytes) -> Dict[str, Any]:
    """
    Pulls the counters out of the health check string ARQ's worker writes.

    The value looks like this:

        ``Aug-25 11:41:20 j_complete=0 j_failed=0 j_retried=0 j_ongoing=0
    queued=0``

    :param raw: the health check key's value.
    """
    # split into tokens by space
    fields = raw.decode(errors="replace").split()

    # get the counter values
    counters = dict(field.split("=", 1) for field in fields if "=" in field)

    def number(name: str) -> int | None:
        try:
            return int(counters[name])
        except (KeyError, ValueError):
            return None

    return {
        # TODO: the date and time format might be an implementation detail
        # that could change underneath our feet in the future
        "last_report": " ".join(field for field in fields if "=" not in field) or None,
        "ongoing": number("j_ongoing"),
        "complete": number("j_complete"),
        "failed": number("j_failed"),
        "retried": number("j_retried"),
    }


class BuildStatusCollector:
    """
    Publishes what the graph builder is doing, read from its status file at scrape time.

    The builder owns that file and may not even be running, so there is nothing to increment from
    the code path here: the file is the state, and a scrape is a read of it.
    """

    def collect(self) -> Iterator[GaugeMetricFamily]:
        try:
            status = json.loads(SETTINGS.get_build_status_path().read_text(encoding="utf8"))
        except (OSError, ValueError):
            return

        state = GaugeMetricFamily(
            "rgp_graph_build_state", "1 on the graph builder's current state.", labels=["state"]
        )
        for value in BuildState:
            state.add_metric([value.value], float(status.get("state") == value.value))
        yield state

        stage = GaugeMetricFamily(
            "rgp_graph_build_stage",
            "1 on the step a running graph build is on, all zero when none is running.",
            labels=["stage"],
        )
        for value in BuildStage:
            stage.add_metric([value.value], float(status.get("stage") == value.value))
        yield stage

        # absent under an external scheduler
        try:
            when = datetime.fromisoformat(status["next_build_at"])
        except (KeyError, TypeError, ValueError):
            return

        yield GaugeMetricFamily(
            "rgp_graph_build_next_timestamp_seconds",
            "When the next graph build is due.",
            value=when.timestamp(),
        )


class WorkerCollector:
    """
    Publishes the worker's queue counters, read from Redis at scrape time.
    """

    def __init__(self):
        self._client: redis.Redis | None = None

    def _redis(self) -> redis.Redis:
        if self._client is None:
            self._client = redis.Redis.from_url(SETTINGS.REDIS_URL, socket_timeout=2)

        return self._client

    def collect(self) -> Iterator[GaugeMetricFamily | CounterMetricFamily]:
        # a collector that raises fails the whole /metrics response, so a missing worker or an
        # unreachable Redis has to yield nothing instead
        try:
            client = self._redis()
            queued = client.zcard(default_queue_name)
            in_progress = sum(1 for _ in client.scan_iter(match=IN_PROGRESS_PATTERN, count=100))
            raw = client.get(WORKER_HEALTH_KEY)
        except Exception:
            self._client = None
            return

        yield GaugeMetricFamily(
            "rgp_worker_queued",
            "Jobs on the queue, waiting and claimed together, as ARQ counts them.",
            value=float(queued),
        )

        yield GaugeMetricFamily(
            "rgp_worker_in_progress",
            "Jobs a worker has claimed. Subtract from rgp_worker_queued for the ones still waiting.",
            value=float(in_progress),
        )

        if not raw:
            return

        health = parse_worker_health(raw)
        if health["ongoing"] is not None:
            yield GaugeMetricFamily(
                "rgp_worker_ongoing",
                "Jobs the worker is running right now.",
                value=float(health["ongoing"]),
            )

        for name, key in (("complete", "complete"), ("failed", "failed"), ("retried", "retried")):
            if health[key] is None:
                continue
            yield CounterMetricFamily(
                f"rgp_worker_jobs_{name}",
                f"Jobs the worker has {name} since it started.",
                value=float(health[key]),
            )


_collectors_registered = False


def register_collectors() -> None:
    """
    Publishes the builder's and the worker's state from this process.

    Only the app calls this. Both collectors read state that is shared between containers, so a
    second process publishing it would only duplicate the same series under another instance.
    """
    global _collectors_registered

    if _collectors_registered:
        return

    REGISTRY.register(BuildStatusCollector())
    REGISTRY.register(WorkerCollector())
    _collectors_registered = True


def start_metrics_server() -> None:
    """
    Serves ``/metrics`` from a background thread, on ``METRICS_PORT``.
    """
    if not SETTINGS.METRICS_PORT:
        return

    start_http_server(SETTINGS.METRICS_PORT)


def endpoint_name(scope: Scope) -> str:
    """
    Names the endpoint a request was routed to, e.g. ``jobs.get_job``. Uses the module and function name
    of the scope's endpoint member.

    :param scope: the request's ASGI scope, after the application handled it.
    """
    endpoint = scope.get("endpoint")
    if endpoint is None:
        return "unknown"

    module = getattr(endpoint, "__module__", "").rsplit(".", 1)[-1]  # use the last one only
    name = getattr(endpoint, "__name__", "unknown")  # fall back to unknown

    return f"{module}.{name}" if module else name


class MetricsMiddleware:
    """
    Counts and times every HTTP request the app serves.

    ASGI middleware, installed into the FastAPI application.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # ignore non-http scopes
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = 500

        # wraps original send
        async def capture_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture_status)
        finally:
            endpoint = endpoint_name(scope)
            HTTP_DURATION.labels(method=scope["method"], endpoint=endpoint).observe(
                time.perf_counter() - started
            )
            HTTP_REQUESTS.labels(method=scope["method"], endpoint=endpoint, status=status).inc()
