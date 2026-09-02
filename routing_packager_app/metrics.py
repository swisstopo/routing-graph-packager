"""
Emits StatsD metrics to whatever collector ``STATSD_HOST`` points at.

DogStatsD rather than plain StatsD: its tag extension is what ``statsd-exporter`` turns into
Prometheus labels, so a route, a status code or a build stage stays a dimension instead of becoming
part of the metric name. Nothing Datadog-hosted is involved.

Metrics are off unless ``STATSD_HOST`` is set, and the client never raises: an unresolvable host or
an unreachable collector drops the packet and logs it. No request, packaging job or graph build can
fail because of a metric.
"""

import os
import time

from datadog.dogstatsd import DogStatsd
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import SETTINGS

METRICS_ENABLED = bool(SETTINGS.STATSD_HOST)

if not METRICS_ENABLED:
    os.environ["DD_DOGSTATSD_DISABLE"] = "true"

METRICS = DogStatsd(
    host=SETTINGS.STATSD_HOST or "localhost",
    port=SETTINGS.STATSD_PORT,
    namespace=SETTINGS.STATSD_PREFIX,
    use_ms=True,
    disable_telemetry=True,
    origin_detection_enabled=False,
)


def endpoint_name(scope: Scope) -> str:
    """
    Names the endpoint a request was routed to, e.g. ``jobs.get_job``.

    The concrete path cannot be the dimension, as every job id would grow its own time series, and
    the route template is not reachable without private API since FastAPI started resolving included
    routers lazily: the ``APIRoute`` on the scope carries its path relative to its own router,
    ``/{job_id}``, not the ``/api/v1/jobs`` it is mounted under. The handler that ran identifies a
    route just as well. It is qualified with its module because two handlers share a name.

    Anything that matched no route at all collapses into one bucket.

    :param scope: the request's ASGI scope, after the application handled it.
    """
    endpoint = scope.get("endpoint")
    if endpoint is None:
        return "unknown"

    module = getattr(endpoint, "__module__", "").rsplit(".", 1)[-1]
    name = getattr(endpoint, "__name__", "unknown")

    return f"{module}.{name}" if module else name


class MetricsMiddleware:
    """
    Counts and times every HTTP request the app serves.

    Plain ASGI rather than Starlette's ``BaseHTTPMiddleware``, which would run the endpoint in a
    separate task just to hand back a response this never looks at.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = 500

        async def capture_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture_status)
        finally:
            tags = [f"method:{scope['method']}", f"endpoint:{endpoint_name(scope)}"]
            METRICS.timing("http.duration", (time.perf_counter() - started) * 1000, tags=tags)
            METRICS.increment("http.requests", tags=tags + [f"status:{status}"])
