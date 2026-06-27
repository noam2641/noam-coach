"""HTTP request guards shared by all private API routes."""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from config import SETTINGS


class APIBodyLimitMiddleware:
    """Enforce a streamed body limit for private API requests."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        if scope.get("type") != "http" or not (
            path.startswith("/api/") or path.startswith("/mini/")
        ):
            await self.app(scope, receive, send)
            return
        limit_bytes = (
            SETTINGS.health_import_max_upload_mb * 1024 * 1024
            if path == "/mini/upload"
            else SETTINGS.api_max_body_bytes
        )

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length:
            try:
                if int(raw_length.decode("ascii")) > limit_bytes:
                    await JSONResponse(
                        status_code=413,
                        content={"detail": "Request body too large"},
                    )(scope, receive, send)
                    return
            except (ValueError, TypeError):
                await JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid Content-Length"},
                )(scope, receive, send)
                return

        buffered: deque[dict[str, Any]] = deque()
        total = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message.get("type") == "http.request":
                total += len(message.get("body", b""))
                if total > limit_bytes:
                    await JSONResponse(
                        status_code=413,
                        content={"detail": "Request body too large"},
                    )(scope, receive, send)
                    return
                if not message.get("more_body", False):
                    break
            elif message.get("type") == "http.disconnect":
                break

        async def replay_receive() -> dict[str, Any]:
            if buffered:
                return buffered.popleft()
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)


class FixedWindowRateLimiter:
    """Small single-process fixed-window limiter for private device APIs."""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()
        self._calls = 0

    async def allow(self, key: str, limit: int, window_seconds: int = 60) -> bool:
        now = time.monotonic()
        cutoff = now - window_seconds
        async with self._lock:
            self._calls += 1
            if self._calls % 100 == 0:
                for stale_key, events in list(self._events.items()):
                    while events and events[0] <= cutoff:
                        events.popleft()
                    if not events:
                        self._events.pop(stale_key, None)

            bucket = self._events[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            return True


API_RATE_LIMITER = FixedWindowRateLimiter()


async def protect_private_api(request: Request, call_next: Callable[..., Any]) -> Any:
    """Apply rate limits and security response headers."""
    if request.url.path.startswith("/api/") or request.url.path.startswith("/mini/"):
        forwarded = request.headers.get("x-forwarded-for", "")
        host = (
            forwarded.split(",", 1)[0].strip()
            if forwarded
            else (request.client.host if request.client else "unknown")
        )
        if not await API_RATE_LIMITER.allow(
            host,
            SETTINGS.api_rate_limit_requests_per_minute,
        ):
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests"},
                headers={"Retry-After": "60"},
            )

    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


def install_security(app: FastAPI) -> None:
    """Attach body-limit and private-API middleware exactly once."""
    app.add_middleware(APIBodyLimitMiddleware)
    app.middleware("http")(protect_private_api)
