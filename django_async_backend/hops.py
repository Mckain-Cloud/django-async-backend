"""Report sync ORM use from a worker thread during an async request.

Every ``sync_to_async()`` hop that opens a sync database cursor while an
event loop is driving the request runs on the sync connection pool: another
connection, outside the request's ``async_atomic()`` block, blocking a
thread. These hops are easy to miss (a template tag, a signal receiver, a
filter backend, a model method called from sync code). With
``ASYNC_BACKEND_SYNC_HOPS = "warn"`` each distinct call site is logged once,
with the first application frames of its stack, on the
``django_async_backend.hops`` logger; ``"error"`` raises
``SyncHopError`` instead. Default ``"off"``.
"""

import logging
import os
import threading
import traceback

from asgiref.sync import SyncToAsync
from django.db.backends.base.base import BaseDatabaseWrapper

logger = logging.getLogger("django_async_backend.hops")

_seen = set()
_lock = threading.Lock()
_original_cursor = BaseDatabaseWrapper.cursor
_installed = False
_policy = "off"

_SKIP = (
    os.sep + "asgiref" + os.sep,
    os.sep + "django" + os.sep,
    os.sep + "django_async_backend" + os.sep,
    os.sep + "concurrent" + os.sep,
    os.sep + "asyncio" + os.sep,
    "threading.py",
    "contextvars",
)


class SyncHopError(RuntimeError):
    pass


def in_sync_hop():
    """Is this thread serving a ``sync_to_async()`` call from an event loop?"""
    return getattr(SyncToAsync.threadlocal, "main_event_loop", None) is not None


def _site(frames):
    app = [f for f in frames if not any(s in f.filename for s in _SKIP)]
    chosen = (app or frames)[-3:]
    return " < ".join(
        "%s:%d %s" % (os.path.basename(f.filename), f.lineno, f.name)
        for f in reversed(chosen)
    )


def _cursor(self, *args, **kwargs):
    if in_sync_hop():
        frames = traceback.extract_stack()[:-1]
        site = _site(frames)
        with _lock:
            first = site not in _seen
            _seen.add(site)
        message = (
            "sync ORM used in a worker thread during an async request "
            "(connection %r): %s" % (self.alias, site)
        )
        if _policy == "error":
            raise SyncHopError(message)
        if first:
            logger.warning(message)
    return _original_cursor(self, *args, **kwargs)


def install(policy):
    global _installed, _policy
    _policy = policy
    if policy == "off" or _installed:
        return
    BaseDatabaseWrapper.cursor = _cursor
    _installed = True


def uninstall():
    global _installed
    BaseDatabaseWrapper.cursor = _original_cursor
    _installed = False
    _seen.clear()
