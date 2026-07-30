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
import os
import time

from django.core.cache import cache

DEFAULT_LIMIT = 60  # requests per window, per IP
DEFAULT_WINDOW = 60  # seconds

# How many appending reverse-proxy hops sit in front of this service. Production
# has two: the system Apache that terminates TLS, then the dockerised Apache that
# proxies into this container. Override if that chain changes (an added CDN or
# load balancer makes it 3; a direct uvicorn with no proxy makes it 1).
TRUSTED_PROXY_HOPS = int(os.environ.get("HIPPIE_MCP_TRUSTED_PROXY_HOPS", "2"))

_CACHE_PREFIX = "mcp-ratelimit"


def client_ip(scope: dict) -> str:
    """Best-effort client IP for an ASGI scope.

    ``X-Forwarded-For`` is read from the right, not the left. Both Apache hops in
    front of this service *append* to the header (default ``ProxyAddHeaders On``)
    rather than replacing it, which makes the entries fall into three groups:

    * The **leftmost** entry is whatever the caller sent. It survives every hop
      untouched, so a client that supplies its own ``X-Forwarded-For`` controls
      it — rotating that value would defeat the cap completely.
    * The **rightmost** entry is the peer of the innermost hop: the outer Apache
      on the same host, i.e. a loopback or docker-bridge address that is the same
      for all traffic. Keying on it would put every request in one shared bucket.
    * ``TRUSTED_PROXY_HOPS`` **from the right** is the entry the outermost,
      internet-facing hop appended — the real client, and the only entry no
      caller can forge.

    Falls back to the leftmost entry when the header is shorter than the expected
    hop count (local dev, or one proxy instead of two) and to the transport peer
    when there is no header at all.
    """
    for name, value in scope.get("headers", ()):
        if name == b"x-forwarded-for":
            parts = [p.strip() for p in value.decode("latin-1").split(",") if p.strip()]
            if len(parts) >= TRUSTED_PROXY_HOPS:
                return parts[-TRUSTED_PROXY_HOPS]
            if parts:
                return parts[0]
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
