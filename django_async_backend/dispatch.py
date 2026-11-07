"""Run async signal receivers in the sender's task; police sync ones.

``Signal.asend()`` / ``asend_robust()`` execute asynchronous receivers
concurrently, each in its own task. With this backend a connection belongs to
the task that created it, so such receivers cannot use the sender's
connection, and anything they write on a connection of their own is outside
the sender's transaction. That breaks every "do something related on
``post_save``" receiver, and even Django's own ``last_login`` update.

For database-bound receivers concurrency is worthless anyway: they would
serialize on the single connection. So the backend replaces Django's parallel
runner with a sequential one that awaits each receiver in the calling task,
in registration order. Applied on startup unless
``ASYNC_BACKEND_SEQUENTIAL_SIGNAL_RECEIVERS`` is ``False``.

Sync receivers still run through ``sync_to_async()`` on the sync connection,
i.e. outside the async transaction, where they cannot see the row they were
notified about. ``ASYNC_BACKEND_SYNC_RECEIVERS`` decides what happens when
that occurs *inside an ``async_atomic()`` block*: ``"warn"`` (default) emits
``SyncReceiverWarning`` once per receiver, ``"error"`` raises
``SyncReceiverInTransaction``, ``"thread"`` keeps Django's behaviour quietly.
"""

import warnings

from django.conf import settings
from django.dispatch import dispatcher

_original_run_parallel = dispatcher._run_parallel
_original_sync_to_async = dispatcher.sync_to_async
_reported = set()


class SyncReceiverWarning(RuntimeWarning):
    pass


class SyncReceiverInTransaction(RuntimeError):
    pass


async def _run_sequential(*coros):
    """Await ``coros`` one after another in the current task."""
    coros = list(coros)
    results = []
    try:
        while coros:
            results.append(await coros.pop(0))
    finally:
        # A failing receiver must not leave the rest "never awaited".
        for coro in coros:
            coro.close()
    return results


def _in_async_transaction():
    from django_async_backend.db import async_connections

    return any(
        conn.in_atomic_block
        for conn in async_connections.all(initialized_only=True)
    )


def _closure(fn, name):
    code = getattr(fn, "__code__", None)
    cells = getattr(fn, "__closure__", None)
    if code is None or not cells or name not in code.co_freevars:
        return None
    return cells[code.co_freevars.index(name)].cell_contents


def _describe(receiver):
    return "%s.%s" % (
        getattr(receiver, "__module__", "?"),
        getattr(receiver, "__qualname__", repr(receiver)),
    )


def _police(sync_send):
    """Apply ``ASYNC_BACKEND_SYNC_RECEIVERS`` to Django's ``sync_send``."""
    policy = getattr(settings, "ASYNC_BACKEND_SYNC_RECEIVERS", "warn")
    if policy == "thread" or not _in_async_transaction():
        return
    receivers = _closure(sync_send, "sync_receivers") or []
    signal = _closure(sync_send, "self")
    for receiver in receivers:
        key = (id(signal), id(receiver))
        message = (
            "Sync receiver %s runs in a worker thread on the sync connection, "
            "outside the async transaction it was fired from (signal %r). Make "
            "it `async def` to run it in the sender's task, or set "
            "ASYNC_BACKEND_SYNC_RECEIVERS = 'thread' to accept this."
            % (_describe(receiver), signal)
        )
        if policy == "error":
            raise SyncReceiverInTransaction(message)
        if key not in _reported:
            _reported.add(key)
            warnings.warn(message, SyncReceiverWarning, stacklevel=4)


def _policed_sync_to_async(fn, *args, **kwargs):
    wrapped = _original_sync_to_async(fn, *args, **kwargs)
    if getattr(fn, "__name__", "") != "sync_send":
        return wrapped

    async def send():
        _police(fn)
        return await wrapped()

    return send


def patch_signal_dispatch():
    dispatcher._run_parallel = _run_sequential
    dispatcher.sync_to_async = _policed_sync_to_async


def unpatch_signal_dispatch():
    dispatcher._run_parallel = _original_run_parallel
    dispatcher.sync_to_async = _original_sync_to_async
    _reported.clear()


def is_sequential():
    return dispatcher._run_parallel is _run_sequential


__all__ = [
    "SyncReceiverInTransaction",
    "SyncReceiverWarning",
    "is_sequential",
    "patch_signal_dispatch",
    "unpatch_signal_dispatch",
]
