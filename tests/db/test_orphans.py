import asyncio
import contextvars
import warnings

from test_app.models import TestModel

from django_async_backend.db import (
    async_acquire,
    async_connections,
)
from django_async_backend.db.orphans import (
    UnclosedConnectionWarning,
    _closing,
)
from django_async_backend.db.transaction import async_atomic
from django_async_backend.test import AsyncioTransactionTestCase


async def _drain_closers():
    # The reaper runs from the finished task's done callback, then closes in
    # a task of its own.
    for _ in range(5):
        await asyncio.sleep(0)
    if _closing:
        await asyncio.gather(*list(_closing))


def _isolated(coro):
    """Run ``coro`` in a new task that sees none of the caller's
    connections: the situation of a task spawned outside any request, before
    the caller touched the alias."""
    return asyncio.create_task(coro, context=contextvars.Context())


class OrphanedConnectionTests(AsyncioTransactionTestCase):
    async def test_unclosed_connection_is_closed_when_its_task_ends(self):
        async def child():
            await TestModel.async_objects.acount()
            return async_connections["default"]

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", UnclosedConnectionWarning)
            wrapper = await _isolated(child())
            self.assertIsNotNone(wrapper.connection)
            await _drain_closers()

        self.assertIsNone(wrapper.connection)
        messages = [
            str(w.message)
            for w in caught
            if issubclass(w.category, UnclosedConnectionWarning)
        ]
        self.assertEqual(len(messages), 1)
        self.assertIn("alias 'default'", messages[0])
        self.assertIn("async_acquire()", messages[0])

    async def test_open_transaction_is_rolled_back(self):
        async def child():
            atomic = async_atomic()
            await atomic.__aenter__()  # never exited
            await TestModel.async_objects.acreate(name="orphan")
            return async_connections["default"]

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", UnclosedConnectionWarning)
            wrapper = await _isolated(child())
            await _drain_closers()

        self.assertTrue(wrapper.closed_in_transaction)
        self.assertIn("rolling back", str(caught[0].message))
        self.assertFalse(
            await TestModel.async_objects.filter(name="orphan").aexists()
        )

    async def test_async_acquire_closes_without_warning(self):
        async def child():
            async with async_acquire() as conn:
                await TestModel.async_objects.acount()
            return conn

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", UnclosedConnectionWarning)
            wrapper = await _isolated(child())
            await _drain_closers()

        self.assertIsNone(wrapper.connection)
        self.assertEqual(
            [w for w in caught if w.category is UnclosedConnectionWarning], []
        )

    async def test_closed_connection_is_not_warned_about(self):
        async def child():
            await TestModel.async_objects.acount()
            conn = async_connections["default"]
            await conn.close()
            return conn

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", UnclosedConnectionWarning)
            await _isolated(child())
            await _drain_closers()

        self.assertEqual(
            [w for w in caught if w.category is UnclosedConnectionWarning], []
        )

    async def test_discarded_connections_leave_no_callbacks_behind(self):
        # A long-lived task (a websocket loop) acquiring a connection per
        # operation must not collect one done-callback per connection.
        async def child():
            wrappers = []
            for _ in range(3):
                async with async_acquire() as conn:
                    await TestModel.async_objects.acount()
                    wrappers.append(conn)
            return wrappers

        wrappers = await _isolated(child())
        for conn in wrappers:
            self.assertNotIn("_owner_watch", vars(conn))

    async def test_connection_handed_to_another_task_is_not_reaped(self):
        # What the test harnesses do between phases.
        async def child():
            await TestModel.async_objects.acount()
            conn = async_connections["default"]
            conn._task = None
            return conn

        wrapper = await _isolated(child())
        await _drain_closers()
        self.assertIsNotNone(wrapper.connection)
        await wrapper.close()

    async def test_gather_of_bare_children_is_cleaned_up(self):
        async def child():
            await TestModel.async_objects.acount()
            return async_connections["default"]

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always", UnclosedConnectionWarning)
            wrappers = await asyncio.gather(
                *(_isolated(child()) for _ in range(3))
            )
            await _drain_closers()

        self.assertEqual(len({id(w) for w in wrappers}), 3)
        self.assertTrue(all(w.connection is None for w in wrappers))
