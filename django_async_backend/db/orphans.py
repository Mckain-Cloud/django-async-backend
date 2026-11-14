"""
Close async connections whose owning task has finished.

A connection belongs to the asyncio task that created it, and only that task
may use it. When a task creates a connection and nobody closes it, the
connection outlives the task: no request middleware sees it (it lives in the
finished task's copy of the context, invisible to the parent), so it stays
open until the wrapper is garbage-collected. With a pool, a collected
connection is never returned, so a fan-out of bare child tasks drains the
pool for good::

    async def child():
        await Book.async_objects.acount()     # first touch: a new connection

    await asyncio.gather(child(), child())    # two connections left behind

asyncio records no parent task, so the creation itself can't be refused.
Instead every connection made by ``async_connections.create_connection()`` is
watched: when its owner finishes while the connection is still open and
still owned by it, the connection is closed (returned to the pool, any open
transaction rolled back) and an ``UnclosedConnectionWarning`` names the task.
The fix in application code is ``async with async_acquire():`` around the
child's work, which closes the connection when the block exits.

Not reaped: connections a test harness keeps across phases. Those are either
inside a test-case transaction (``_from_testcase``, the same exemption as
``close_old_async_connections()``) or released (``_task = None``) before the
phase task finishes. A claimed connection (a released wrapper picked up by
the next task in ``validate_task_sharing()``) is not watched again.
"""

import asyncio
import functools
import warnings
import weakref


class UnclosedConnectionWarning(RuntimeWarning):
    """An async connection was still open when its owning task finished."""


# Close tasks in flight, kept so they aren't garbage-collected mid-close.
_closing = set()


def watch_owner(connection, task):
    """Close ``connection`` when ``task`` finishes, if it is still open and
    still owned by ``task`` then."""
    unwatch_owner(connection)
    callback = functools.partial(_owner_finished, weakref.ref(connection))
    task.add_done_callback(callback)
    connection._owner_watch = (task, callback)


def unwatch_owner(connection):
    """Stop watching ``connection`` (it is being discarded and was closed).

    Long-lived tasks that open many short connections (``async_acquire()``
    in a websocket loop) would otherwise collect one callback per connection.
    """
    watch = connection.__dict__.pop("_owner_watch", None)
    if watch is not None:
        task, callback = watch
        task.remove_done_callback(callback)


def _owner_finished(connection_ref, task):
    connection = connection_ref()
    if connection is None:
        return
    watch = connection.__dict__.get("_owner_watch")
    if watch is None or watch[0] is not task:
        return
    del connection.__dict__["_owner_watch"]
    if not _is_orphan(connection, task):
        return

    loop = task.get_loop()
    if loop.is_closed():
        return
    in_transaction = connection.in_atomic_block
    warnings.warn(
        "The async connection for alias '%s' was still open when its owning "
        "task %r finished; closing it%s. Wrap work that runs in its own task "
        "(asyncio.create_task(), gather(), TaskGroup) outside a request in "
        "`async with async_acquire():` so the connection is closed when the "
        "work is done."
        % (
            connection.alias,
            task.get_name(),
            " and rolling back its open transaction" if in_transaction else "",
        ),
        UnclosedConnectionWarning,
        stacklevel=1,
    )
    closer = loop.create_task(_aclose_orphan(connection, task))
    _closing.add(closer)
    closer.add_done_callback(_closing.discard)


def _is_orphan(connection, task):
    from django_async_backend.db import _in_testcase_transaction

    return (
        connection._task is task
        and connection.connection is not None
        and not _in_testcase_transaction(connection)
    )


async def _aclose_orphan(connection, task):
    # Something may have adopted the connection since the callback ran.
    if not _is_orphan(connection, task):
        return
    await connection.close()
