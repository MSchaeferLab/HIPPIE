"""Tests for the MCP server's ASGI entry point and its rate limiter.

``test_mcp_protocol`` and ``test_mcp_tools`` both talk to the ``mcp`` server
object directly — in-memory, no transport. That leaves everything in
:mod:`hippie_mcp.asgi` and :mod:`hippie_mcp.ratelimit` uncovered, and all of it
fails in a way that takes out *every* request rather than degrading:

* no lifespan → ``RuntimeError: Task group is not initialized`` on every call
* a wrong Host allowlist → ``421 Misdirected Request`` on every call
* a spoofable rate-limit key → the cap is decorative

So these boot the real Starlette ``app``. ``TestClient`` drives the ASGI lifespan
(that is what the ``with`` block is for) and is sync-callable, so these stay
ordinary ``SimpleTestCase`` methods.

The default ``TestClient`` host is ``testserver``, which
``django.test.utils.setup_test_environment`` appends to ``ALLOWED_HOSTS`` before
test modules are imported — so the allowlist ``asgi`` builds at import time
always contains it.
"""

from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from hippie_mcp import ratelimit
from hippie_mcp.asgi import _transport_security, app
from hippie_mcp.ratelimit import RateLimitMiddleware, client_ip

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "hippie-tests", "version": "0"},
    },
}
# The streamable-HTTP transport requires the client to accept both.
MCP_HEADERS = {"accept": "application/json, text/event-stream"}

# A long window so a request batch cannot straddle two fixed-window buckets and
# make a limit assertion flaky.
LONG_WINDOW = 3600


class AsgiAppTests(SimpleTestCase):
    """The real app, booted through its real lifespan.

    Booted once for the whole class, not once per test:
    ``StreamableHTTPSessionManager.run()`` raises if it is entered twice on the
    same instance, and ``mcp`` is a module-level singleton. One boot per process
    is also what production does.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Not `cls.client`: SimpleTestCase assigns Django's own test client to
        # that name before every test, which would silently redirect these
        # requests at the Django URLconf and make them pass vacuously.
        cls.asgi = cls.enterClassContext(TestClient(app))

    def test_health_answers_and_touches_no_database(self):
        resp = self.asgi.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok", "service": "hippie-mcp"})

    def test_mcp_endpoint_answers_through_the_real_app(self):
        """The one test that proves the lifespan is wired to the top-level app.

        Without ``lifespan`` entering ``mcp.session_manager.run()`` this is a 500
        with "Task group is not initialized"; with a localhost-only allowlist it
        is a 421. Both are the whole-service outages this file exists to catch.
        """
        resp = self.asgi.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
        self.assertEqual(resp.status_code, 200)
        # The transport answers initialize as an SSE stream carrying the result.
        self.assertIn("text/event-stream", resp.headers["content-type"])
        self.assertIn('"result"', resp.text)

    def test_mcp_endpoint_is_served_without_a_redirect(self):
        """``/mcp`` must answer directly.

        It is mounted at the root with the path on the inner app so that the
        documented URL is the one that answers — a 307 to ``/mcp/`` would drop the
        body for clients that do not re-send it on redirect.
        """
        resp = self.asgi.post(
            "/mcp", json=INITIALIZE, headers=MCP_HEADERS, follow_redirects=False
        )
        self.assertEqual(resp.status_code, 200)

    def test_unknown_host_is_rejected(self):
        """Proves DNS-rebinding protection is armed, not merely configured."""
        resp = self.asgi.post(
            "/mcp",
            json=INITIALIZE,
            headers={**MCP_HEADERS, "host": "evil.example.com"},
        )
        self.assertEqual(resp.status_code, 421)


class TransportSecurityTests(SimpleTestCase):
    """``_transport_security`` reads settings at call time, so it can be tested
    independently of the allowlist baked into the imported ``app``."""

    @override_settings(
        ALLOWED_HOSTS=["hippie.example.org"],
        CSRF_TRUSTED_ORIGINS=["https://hippie.example.org"],
    )
    def test_allowed_hosts_are_registered_bare_and_with_a_port_wildcard(self):
        settings_obj = _transport_security()
        self.assertEqual(
            settings_obj.allowed_hosts,
            ["hippie.example.org", "hippie.example.org:*"],
        )
        self.assertEqual(settings_obj.allowed_origins, ["https://hippie.example.org"])

    @override_settings(ALLOWED_HOSTS=["hippie.example.org"], CSRF_TRUSTED_ORIGINS=[])
    def test_origins_default_to_https_on_the_allowed_hosts(self):
        self.assertEqual(
            _transport_security().allowed_origins, ["https://hippie.example.org"]
        )

    @override_settings(ALLOWED_HOSTS=[])
    def test_no_allowed_hosts_keeps_the_sdk_localhost_default(self):
        self.assertIsNone(_transport_security())

    @override_settings(ALLOWED_HOSTS=["*"])
    def test_a_wildcard_host_disables_rebinding_protection(self):
        """``'*'`` means "any host" to Django but nothing to the SDK matcher.

        Registered verbatim it would allow exactly one host named ``*`` and reject
        every real one; dropped, it would fall back to localhost-only and do the
        same. The operator asked for any host, so protection goes off instead.
        """
        settings_obj = _transport_security()
        self.assertIsNotNone(settings_obj)
        self.assertFalse(settings_obj.enable_dns_rebinding_protection)

    @override_settings(ALLOWED_HOSTS=[".example.com"], CSRF_TRUSTED_ORIGINS=[])
    def test_a_leading_dot_becomes_the_domain_plus_a_subdomain_wildcard(self):
        """Django's ``.example.com`` covers the domain and any subdomain; the SDK
        matcher has no such form, so both spellings are registered."""
        allowed = _transport_security().allowed_hosts
        self.assertEqual(
            allowed,
            ["example.com", "example.com:*", "*.example.com", "*.example.com:*"],
        )


def _ping(request) -> JSONResponse:
    return JSONResponse({"ok": True})


def _limited_app(limit: int, window: int = LONG_WINDOW) -> Starlette:
    """A one-route app behind the middleware, so a low limit is cheap to reach."""
    return Starlette(
        routes=[Route("/ping", _ping, methods=["GET"])],
        middleware=[Middleware(RateLimitMiddleware, limit=limit, window=window)],
    )


class _DeadCache:
    """Stands in for a Redis that has gone away mid-request."""

    async def aincr(self, key):
        raise ConnectionError("cache down")

    async def aset(self, *args, **kwargs):
        raise ConnectionError("cache down")


class RateLimitMiddlewareTests(SimpleTestCase):
    def setUp(self):
        # The counter lives in the process-wide locmem cache in tests, so one
        # test's requests would otherwise count against the next one's budget.
        cache.clear()
        self.addCleanup(cache.clear)

    def test_requests_under_the_limit_pass_through(self):
        with TestClient(_limited_app(limit=3)) as client:
            codes = [client.get("/ping").status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 200])

    def test_over_the_limit_returns_429_with_retry_after(self):
        with TestClient(_limited_app(limit=2)) as client:
            self.assertEqual(client.get("/ping").status_code, 200)
            self.assertEqual(client.get("/ping").status_code, 200)
            resp = client.get("/ping")
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.headers["retry-after"], str(LONG_WINDOW))
        self.assertEqual(resp.json()["error"], "rate_limited")

    def test_separate_clients_get_separate_budgets(self):
        with TestClient(_limited_app(limit=1)) as client:
            first = client.get("/ping", headers={"x-forwarded-for": "9.9.9.9, 1.1.1.1"})
            other = client.get("/ping", headers={"x-forwarded-for": "8.8.8.8, 1.1.1.1"})
        self.assertEqual((first.status_code, other.status_code), (200, 200))

    def test_a_spoofed_leftmost_entry_cannot_win_a_fresh_budget(self):
        """The bypass this middleware was fixed for.

        Only the leftmost entry is caller-controlled. Rotating it must not move
        the request to a different bucket, or the cap means nothing.
        """
        with (
            patch.object(ratelimit, "TRUSTED_PROXY_HOPS", 2),
            TestClient(_limited_app(limit=1)) as client,
        ):
            first = client.get(
                "/ping", headers={"x-forwarded-for": "spoof-1, 203.0.113.7, 172.18.0.1"}
            )
            second = client.get(
                "/ping", headers={"x-forwarded-for": "spoof-2, 203.0.113.7, 172.18.0.1"}
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)

    def test_a_non_http_scope_passes_through_untouched(self):
        """The lifespan and websocket guard.

        ``limit=0`` rejects any HTTP request, so reaching the inner app at all
        proves the scope check short-circuited before any counting happened.
        """
        seen: list[str] = []

        async def inner(scope, receive, send):
            seen.append(scope["type"])

        async def receive():  # pragma: no cover - never awaited
            return {"type": "lifespan.startup"}

        async def send(message):  # pragma: no cover - never called
            raise AssertionError(f"middleware sent {message!r} for a lifespan scope")

        middleware = RateLimitMiddleware(inner, limit=0, window=LONG_WINDOW)
        async_to_sync(middleware)({"type": "lifespan"}, receive, send)
        self.assertEqual(seen, ["lifespan"])

    def test_a_cache_outage_fails_open(self):
        """A dead cache must not take the endpoint down with it."""
        with patch.object(ratelimit, "cache", _DeadCache()):
            with TestClient(_limited_app(limit=1)) as client:
                codes = [client.get("/ping").status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 200])


class ClientIpTests(SimpleTestCase):
    """Which ``X-Forwarded-For`` entry the limiter keys on.

    Both proxy hops in production append rather than replace, so the header reads
    ``<caller-supplied>, <real client>, <inner hop's peer>``.
    """

    @staticmethod
    def _scope(xff: str | None, *, client=("172.18.0.1", 4000)) -> dict:
        headers = [(b"host", b"testserver")]
        if xff is not None:
            headers.append((b"x-forwarded-for", xff.encode()))
        return {"type": "http", "headers": headers, "client": client}

    def test_two_hops_picks_the_entry_the_outer_proxy_appended(self):
        with patch.object(ratelimit, "TRUSTED_PROXY_HOPS", 2):
            ip = client_ip(self._scope("198.51.100.9, 203.0.113.7, 172.18.0.1"))
        self.assertEqual(ip, "203.0.113.7")

    def test_the_rightmost_entry_is_not_used(self):
        """It is the inner hop's own peer — constant for all production traffic,
        so keying on it would put every caller in one shared bucket."""
        with patch.object(ratelimit, "TRUSTED_PROXY_HOPS", 2):
            ip = client_ip(self._scope("198.51.100.9, 203.0.113.7, 172.18.0.1"))
        self.assertNotEqual(ip, "172.18.0.1")

    def test_hop_count_is_configurable(self):
        header = "198.51.100.9, 203.0.113.7, 172.18.0.1"
        for hops, expected in (
            (1, "172.18.0.1"),
            (2, "203.0.113.7"),
            (3, "198.51.100.9"),
        ):
            with (
                self.subTest(hops=hops),
                patch.object(ratelimit, "TRUSTED_PROXY_HOPS", hops),
            ):
                self.assertEqual(client_ip(self._scope(header)), expected)

    def test_fewer_entries_than_expected_falls_back_to_the_leftmost(self):
        """One proxy instead of two — dev, or a direct uvicorn. Best effort beats
        no key at all, and there is no untrusted hop to protect against here."""
        with patch.object(ratelimit, "TRUSTED_PROXY_HOPS", 2):
            self.assertEqual(client_ip(self._scope("203.0.113.7")), "203.0.113.7")

    def test_whitespace_and_empty_entries_are_ignored(self):
        with patch.object(ratelimit, "TRUSTED_PROXY_HOPS", 2):
            ip = client_ip(self._scope(" 198.51.100.9 ,  203.0.113.7 , , 172.18.0.1 "))
        self.assertEqual(ip, "203.0.113.7")

    def test_no_header_uses_the_transport_peer(self):
        self.assertEqual(client_ip(self._scope(None)), "172.18.0.1")

    def test_no_header_and_no_peer_is_unknown(self):
        self.assertEqual(client_ip(self._scope(None, client=None)), "unknown")
