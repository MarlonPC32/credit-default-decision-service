"""Bounded HTTP metrics and structured operational logs."""

import json
import logging
from datetime import datetime, timezone
from time import perf_counter

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REGISTRY = CollectorRegistry()
REQUESTS = Counter(
    "credit_http_requests_total", "Completed HTTP requests.",
    ("method", "route", "status"), registry=REGISTRY,
)
DURATION = Histogram(
    "credit_http_request_duration_seconds", "HTTP request duration.",
    ("method", "route"), registry=REGISTRY,
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
LOGGER = logging.getLogger("credit_default.http")
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False
LOGGER.addHandler(logging.StreamHandler())


def metrics() -> Response:
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


class HttpTelemetry:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == "/metrics":
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        route = path if path in ("/health", "/predict") else "other"
        method = scope["method"] if scope["method"] in ("GET", "HEAD", "POST") else "OTHER"
        started = perf_counter()
        status = 500

        async def record_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, record_status)
        finally:
            duration_seconds = perf_counter() - started
            REQUESTS.labels(method, route, str(status)).inc()
            DURATION.labels(method, route).observe(duration_seconds)
            if route != "/health" or status >= 400:
                level = logging.WARNING if status >= 500 else logging.INFO
                LOGGER.log(level, json.dumps({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "level": logging.getLevelName(level),
                    "event": "http_request",
                    "service": "fastapi",
                    "method": method,
                    "route": route,
                    "status": status,
                    "duration_seconds": duration_seconds,
                }))
