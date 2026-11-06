import asyncio

from django.dispatch import (
    Signal,
    dispatcher,
)
from test_app.models import TestModel

from django_async_backend.db.transaction import async_atomic
from django_async_backend.dispatch import (
    _original_run_parallel,
    is_sequential,
    patch_signal_dispatch,
    unpatch_signal_dispatch,
)
from django_async_backend.test import AsyncioTestCase


class SequentialSignalDispatchTests(AsyncioTestCase):
    async def test_patched_on_startup(self):
        self.assertTrue(is_sequential())

    async def test_async_receivers_run_in_the_senders_task_in_order(self):
        signal = Signal()
        log = []
        sender_task = asyncio.current_task()

        async def first(**kwargs):
            log.append(("first", asyncio.current_task() is sender_task))
            await asyncio.sleep(0.01)
            log.append("first-done")

        async def second(**kwargs):
            log.append(("second", asyncio.current_task() is sender_task))

        signal.connect(first)
        signal.connect(second)
        responses = await signal.asend(sender=self)

        self.assertEqual(
            log, [("first", True), "first-done", ("second", True)]
        )
        self.assertEqual([r for r, _ in responses], [first, second])

    async def test_receivers_see_the_senders_transaction(self):
        signal = Signal()
        seen = []

        async def receiver(**kwargs):
            seen.append(await TestModel.async_objects.acount())

        signal.connect(receiver)
        async with async_atomic():
            await TestModel.async_objects.acreate(name="uncommitted", value=1)
            await signal.asend(sender=self)
        self.assertEqual(seen, [1])

    async def test_failing_receiver_closes_the_rest(self):
        signal = Signal()
        ran = []

        async def boom(**kwargs):
            raise ValueError("boom")

        async def never(**kwargs):
            ran.append("never")

        signal.connect(boom)
        signal.connect(never)
        with self.assertRaises(ValueError):
            await signal.asend(sender=self)
        self.assertEqual(ran, [])

        responses = await signal.asend_robust(sender=self)
        self.assertIsInstance(responses[0][1], ValueError)
        self.assertEqual(ran, ["never"])

    async def test_unpatch_restores_django(self):
        try:
            unpatch_signal_dispatch()
            self.assertFalse(is_sequential())
            self.assertIs(dispatcher._run_parallel, _original_run_parallel)
        finally:
            patch_signal_dispatch()
        self.assertTrue(is_sequential())
