"""ASGI entry point for the HIPPIE MCP server.

Run with::

    uvicorn hippie_mcp.asgi:app --host 0.0.0.0 --port 8001

from the ``hippie_django`` directory. Django is configured here, before any
module that touches a model is imported, because ``hippie_mcp.server`` imports
``hippie_website.models`` transitively.

Two things about this file are load-bearing and fail in confusing ways if
changed — see the comments at each: the lifespan that starts the session
manager, and the Host allowlist.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hippie.settings")
django.setup()

from django.conf import settings  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.middleware import Middleware  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Mount, Route  # noqa: E402

from hippie_mcp.ratelimit import (  # noqa: E402
    DEFAULT_LIMIT,
    DEFAULT_WINDOW,
    RateLimitMiddleware,
)
from hippie_mcp.server import mcp  # noqa: E402

# Path the MCP endpoint is served at, relative to wherever this app is mounted.
MCP_PATH = os.environ.get("HIPPIE_MCP_PATH", "/mcp")

RATE_LIMIT = int(os.environ.get("HIPPIE_MCP_RATE_LIMIT", DEFAULT_LIMIT))
RATE_WINDOW = int(os.environ.get("HIPPIE_MCP_RATE_WINDOW", DEFAULT_WINDOW))


def _transport_security() -> TransportSecuritySettings | None:
    """Build the Host/Origin allowlist from Django's own settings.

    ``streamable_http_app()`` arms DNS-rebinding protection with a
    localhost-only allowlist, because it cannot know what hostname it will be
    served behind. Behind a real hostname — and Apache forwards the real one, it
    sets ``ProxyPreserveHost On`` — every request is then rejected with
    ``421 Misdirected Request`` before any of our code runs, and the only trace
    is a line in this service's log.

    So the allowlist is derived from ``DJANGO_ALLOWED_HOSTS``, which the web
    container already needs to be correct. Each host is registered both bare and
    with a port wildcard, because the allowlist matches ``host:port`` patterns
    and a ``Host`` header may or may not carry the port.

    Two Django spellings need translating rather than copying, because the SDK
    matcher only understands literal hosts and a trailing ``:*`` port wildcard:

    * ``'*'`` means "any host" to Django. Registered literally it would allow
      exactly one host named ``*`` and reject everything real, so it disables
      DNS-rebinding protection instead — which is what the operator asked for.
    * ``'.example.com'`` means "example.com and any subdomain". The SDK has no
      subdomain wildcard, so it becomes ``example.com`` plus ``*.example.com``.

    Returning ``None`` (no ALLOWED_HOSTS, i.e. local dev) leaves the SDK's
    localhost-only default in place, which is the right answer there.
    """
    raw = [h for h in settings.ALLOWED_HOSTS if h]
    if "*" in raw:
        # Any host is allowed; an allowlist would only be able to narrow that.
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    hosts: list[str] = []
    for host in raw:
        if host.startswith("."):
            hosts.append(host[1:])
            hosts.append(f"*{host}")
        else:
            hosts.append(host)
    if not hosts:
        return None

    allowed_hosts: list[str] = []
    for host in hosts:
        allowed_hosts.append(host)
        allowed_hosts.append(f"{host}:*")

    origins = list(getattr(settings, "CSRF_TRUSTED_ORIGINS", []) or [])
    if not origins:
        origins = [f"https://{host}" for host in hosts]

    return TransportSecuritySettings(
        allowed_hosts=allowed_hosts,
        allowed_origins=origins,
    )


@asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    """Start the MCP session manager.

    ``streamable_http_app()`` wires this into the lifespan of the Starlette it
    returns, but a *mounted* sub-application's lifespan never runs — so that
    wiring is dead code here and the top-level app must do it. Without this the
    service starts cleanly and then fails every single request with
    ``RuntimeError: Task group is not initialized``.
    """
    async with mcp.session_manager.run():
        yield


async def health(request) -> JSONResponse:
    """Liveness probe. Deliberately does not touch the database — it answers
    "is this process up", which is what a proxy needs to know."""
    return JSONResponse({"status": "ok", "service": "hippie-mcp"})


app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        # Mounted at the root with the endpoint's real path on the inner app,
        # rather than Mount(MCP_PATH, ...) with an inner "/". Both serve the
        # endpoint, but the prefix form makes the route "/mcp/" and answers a
        # POST to "/mcp" with a 307 to it. The SDK client follows that; not every
        # MCP client does, and a redirected POST can arrive without its body.
        # The documented URL should be the one that actually answers.
        Mount(
            "/",
            app=mcp.streamable_http_app(
                streamable_http_path=MCP_PATH,
                transport_security=_transport_security(),
            ),
        ),
    ],
    middleware=[
        Middleware(
            RateLimitMiddleware,
            limit=RATE_LIMIT,
            window=RATE_WINDOW,
        )
    ],
    lifespan=lifespan,
)
