"""Run async signal receivers in the sender's task.

``Signal.asend()`` / ``asend_robust()`` execute asynchronous receivers
concurrently, each in its own task. With this backend a connection belongs to
the task that created it, so such receivers cannot use the sender's
connection, and anything they write on a connection of their own is outside
the sender's transaction. That breaks every "do something related on
``post_save``" receiver, and even Django's own ``last_login`` update.

For database-bound receivers concurrency is worthless anyway: they would
serialize on the single connection. So the backend replaces Django's parallel
runner with a sequential one that awaits each receiver in the calling task,
in registration order. Sync receivers are unaffected: ``asend()`` still runs
them through ``sync_to_async()`` on the sync connection, i.e. outside the
async transaction.

Applied on startup unless ``ASYNC_BACKEND_SEQUENTIAL_SIGNAL_RECEIVERS`` is
``False``.
"""

from django.dispatch import dispatcher

_original_run_parallel = dispatcher._run_parallel


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


def patch_signal_dispatch():
    dispatcher._run_parallel = _run_sequential


def unpatch_signal_dispatch():
    dispatcher._run_parallel = _original_run_parallel


def is_sequential():
    return dispatcher._run_parallel is _run_sequential
