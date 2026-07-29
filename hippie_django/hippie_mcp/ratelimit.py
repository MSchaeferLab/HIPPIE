"""Per-IP rate limiting for the public MCP endpoint.

The endpoint is open, matching the website's posture, so the only thing standing
between a runaway agent loop and a 1.15M-row database is a request cap. This is
a coarse fixed-window counter over the Django cache — Redis in production
(``REDIS_CACHE_URL``), local memory in dev — which is per-process and therefore
approximate when several workers run. That is fine: the goal is to stop a loop,
not to meter billing.

Written as pure ASGI middleware rather than Starlette's ``BaseHTTPMiddleware``
on purpose. ``BaseHTTPMiddleware`` buffers through an inner task and interferes
with long-lived streaming responses, which is exactly what MCP's streamable HTTP
transport serves.
"""

import json
import time

from django.core.cache import cache

DEFAULT_LIMIT = 60  # requests per window, per IP
DEFAULT_WINDOW = 60  # seconds

_CACHE_PREFIX = "mcp-ratelimit"


def client_ip(scope: dict) -> str:
    """Best-effort client IP for an ASGI scope.

    Apache sits in front of this service and appends the peer to
    ``X-Forwarded-For``, so the direct peer is always the proxy. The leftmost
    entry is the original client. It is caller-supplied and therefore spoofable
    — acceptable here, because the limiter is abuse dampening, not authentication.
    """
    for name, value in scope.get("headers", ()):
        if name == b"x-forwarded-for":
            first = value.decode("latin-1").split(",")[0].strip()
            if first:
                return first
    client = scope.get("client")
    return client[0] if client else "unknown"


class RateLimitMiddleware:
    """Reject a client that exceeds ``limit`` requests per ``window`` seconds."""

    def __init__(
        self,
        app,
        *,
        limit: int = DEFAULT_LIMIT,
        window: int = DEFAULT_WINDOW,
    ) -> None:
        self.app = app
        self.limit = limit
        self.window = window

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        ip = client_ip(scope)
        bucket = int(time.time() // self.window)
        key = f"{_CACHE_PREFIX}:{ip}:{bucket}"

        try:
            count = await cache.aincr(key)
        except ValueError:
            # No key yet for this window. Two concurrent first requests can both
            # land here and each set 1; a one-request overshoot per window is not
            # worth a lock.
            await cache.aset(key, 1, timeout=self.window * 2)
            count = 1
        except Exception:
            # A cache outage must not take the endpoint down with it — fail open
            # and let the result caps carry the load.
            await self.app(scope, receive, send)
            return

        if count > self.limit:
            await self._reject(send)
            return

        await self.app(scope, receive, send)

    async def _reject(self, send) -> None:
        body = json.dumps(
            {
                "error": "rate_limited",
                "message": (
                    f"Rate limit exceeded ({self.limit} requests per "
                    f"{self.window}s). Retry after the window resets."
                ),
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"retry-after", str(self.window).encode()),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
