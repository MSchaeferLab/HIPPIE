"""Database-connection hygiene for tools running on a worker thread.

The MCP SDK runs a synchronous (``def``) tool in a worker thread via
``anyio.to_thread.run_sync()``. That is what makes the synchronous Django ORM
usable here at all — but it also means tool bodies touch the ORM from threads
Django never set up as request handlers, and Django connections are thread-local.

A Django *request* gets connection cleanup for free: ``close_old_connections``
is wired to the ``request_started`` / ``request_finished`` signals. There are no
requests here. Without the equivalent, a pooled worker thread keeps its
connection open indefinitely, MariaDB eventually closes it at ``wait_timeout``,
and the next tool call on that thread fails with "MySQL server has gone away" —
intermittently, on whichever thread happens to be reused after an idle spell.

:func:`with_db` supplies that cleanup. Wrap every tool body in it.
"""

import functools
from collections.abc import Callable
from typing import ParamSpec, TypeVar

from django.db import connections

P = ParamSpec("P")
R = TypeVar("R")


def _close_idle_connections() -> None:
    """``django.db.close_old_connections()``, but never inside a transaction.

    The stock helper closes any connection it judges unusable or obsolete,
    including one with an open atomic block — and closing that discards the
    transaction. Nothing here opens transactions in production, but Django's
    ``TestCase`` wraps every test in one, so the stock call would throw away the
    test's own fixtures. Skipping in-transaction connections keeps the cleanup
    doing its job without ever being the thing that loses data.
    """
    for conn in connections.all(initialized_only=True):
        if conn.in_atomic_block:
            continue
        conn.close_if_unusable_or_obsolete()


def with_db(fn: Callable[P, R]) -> Callable[P, R]:
    """Close stale/expired DB connections around a tool body.

    Runs on entry as well as exit: on entry because the connection this thread
    inherited may already have timed out, on exit so an idle thread is not
    holding one open.
    """

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        _close_idle_connections()
        try:
            return fn(*args, **kwargs)
        finally:
            _close_idle_connections()

    return wrapper
