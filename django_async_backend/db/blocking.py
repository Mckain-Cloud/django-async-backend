"""Run a database coroutine to completion without suspending the task.

Sync code that runs inside a task (a ``__str__``, a property, a template, a
serializer) cannot ``await``, so it cannot use the task's async connection
the normal way. ``run_blocking()`` gets it the answer anyway: for the
duration of one coroutine, the psycopg connection's ``wait()`` is replaced by
one that drives libpq with psycopg's *blocking* wait on the same socket. No
``await`` in the coroutine then has anything to suspend on, and a single
``coro.send(None)`` runs the whole async ORM stack to its result, on the
task's own connection and inside its transaction.

The price is the event loop: it is frozen until the query returns, and so is
every other request the worker is serving. Task cancellation and
``asyncio.timeout()`` cannot interrupt it either (a server-side
``statement_timeout`` can). This is what backs ``FETCH_BLOCKING`` and
``ASYNC_BACKEND_BLOCKING_QUERYSETS``; both are opt-in.

``ASYNC_BACKEND_BLOCKING_QUERIES`` (``"warn"`` by default, or ``"off"``)
logs each distinct call site once on the ``django_async_backend.blocking``
logger.
"""

import logging
import os
import sys
import threading

from django.conf import settings
from psycopg import waiting

from django_async_backend.db import async_connections

logger = logging.getLogger("django_async_backend.blocking")

# psycopg's own sync connections poll at this interval so Ctrl-C is noticed.
_WAIT_INTERVAL = 0.1

_seen = set()
_lock = threading.Lock()

_SKIP = (
    os.sep + "asgiref" + os.sep,
    os.sep + "django" + os.sep,
    os.sep + "django_async_backend" + os.sep,
    os.sep + "asyncio" + os.sep,
    os.sep + "uvloop" + os.sep,
    os.sep + "unittest" + os.sep,
    "<frozen ",
)


class BlockingQueryError(RuntimeError):
    """A blocking query could not run on the task's connection."""


def _call_site(limit=3):
    """The innermost application frames, without reading any source line."""
    frame = sys._getframe(2)
    found = []
    while frame is not None and len(found) < limit:
        filename = frame.f_code.co_filename
        if not any(skip in filename for skip in _SKIP):
            found.append(
                "%s:%d %s"
                % (
                    os.path.basename(filename),
                    frame.f_lineno,
                    frame.f_code.co_name,
                )
            )
        frame = frame.f_back
    return " < ".join(found) or "<unknown>"


def _report(alias):
    if getattr(settings, "ASYNC_BACKEND_BLOCKING_QUERIES", "warn") == "off":
        return
    site = _call_site()
    with _lock:
        if site in _seen:
            return
        _seen.add(site)
    logger.warning(
        "blocking query on the event loop (connection %r): %s", alias, site
    )


def reset_reported():
    with _lock:
        _seen.clear()


def run_blocking(coro, alias):
    """Run ``coro`` (which may only wait on ``alias``) and return its result.

    Raises ``BlockingQueryError``, without running anything, when the task
    has no open connection for ``alias`` or that connection is in the middle
    of something (pipeline mode, ``cursor.stream()``, ``copy``), and when the
    coroutine tries to wait for anything other than that connection.
    """
    wrapper = async_connections[alias]
    connection = wrapper.connection
    if connection is None:
        coro.close()
        raise BlockingQueryError(
            "This task has no open async connection for %r to run a "
            "blocking query on; run an async query first or use the async "
            "API." % alias
        )
    # Private psycopg API: tests/db/test_blocking.py fails if it moves.
    if connection.lock.locked():
        coro.close()
        raise BlockingQueryError(
            "The async connection for %r is in the middle of another "
            "operation (a streaming cursor or COPY); a blocking query "
            "cannot run on it now." % alias
        )
    if connection._pipeline is not None:
        coro.close()
        raise BlockingQueryError(
            "The async connection for %r is in pipeline mode; a blocking "
            "query cannot run on it now." % alias
        )

    _report(alias)

    async def blocking_wait(gen, interval=_WAIT_INTERVAL, timeout=None):
        return waiting.wait(gen, connection.pgconn.socket, _WAIT_INTERVAL)

    # An instance attribute shadows AsyncConnection.wait; a nested call (a
    # lazy load inside a lazy load) finds one already there and restores it.
    previous = connection.__dict__.get("wait")
    connection.wait = blocking_wait
    try:
        try:
            awaited = coro.send(None)
        except StopIteration as stop:
            return stop.value
        coro.close()
        raise BlockingQueryError(
            "A blocking query can only wait for its own database "
            "connection, but this one tried to wait for %r (a sync signal "
            "receiver, a second database alias or other I/O). Use the async "
            "API here." % (awaited,)
        )
    finally:
        if previous is None:
            del connection.wait
        else:
            connection.wait = previous
