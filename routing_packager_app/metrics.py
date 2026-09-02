"""
Monitoring metrics for th app, the worker and the graph build.

Prometheus is used for the app and the worker (long lived services), while StatsD is used for the
graph builder (which may be an externally controlled one-shot).
"""

import os
import time

from datadog.dogstatsd.base import DogStatsd
from prometheus_client import Counter, Histogram, disable_created_metrics, start_http_server
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import SETTINGS

disable_created_metrics()

METRICS_PATH = "/metrics"

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


def start_metrics_server() -> None:
    """
    Serves ``/metrics`` from a background thread. Used by the worker which mounts
    its own small HTTP server this way.
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
        # ignore metrics end point and non-http scopes
        if scope["type"] != "http" or scope["path"].startswith(METRICS_PATH):
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
