import asyncio

from django.dispatch import (
    Signal,
    dispatcher,
)
from test_app.models import TestModel

from django.db.models.signals import post_save
from django.test import override_settings

from django_async_backend.db.transaction import async_atomic
from django_async_backend.dispatch import (
    SyncReceiverInTransaction,
    SyncReceiverWarning,
    _original_run_parallel,
    is_sequential,
    patch_signal_dispatch,
    unpatch_signal_dispatch,
)
from django_async_backend.test import (
    AsyncioTestCase,
    AsyncioTransactionTestCase,
)


class SyncReceiverPolicyTests(AsyncioTransactionTestCase):
    # A TransactionTestCase: AsyncioTestCase wraps each test in a transaction,
    # which is exactly what the policy looks for.

    def _connect(self, name):
        calls = []

        def receiver(sender, **kwargs):
            calls.append(kwargs["instance"].name)

        receiver.__qualname__ = name
        post_save.connect(receiver, sender=TestModel, dispatch_uid=name, weak=False)
        self.addCleanup(post_save.disconnect, dispatch_uid=name, sender=TestModel)
        return calls

    async def asyncTearDown(self):
        from tests.contrib.test_admin import _close_sync_connections

        await _close_sync_connections()
        await TestModel.async_objects.all().adelete()

    async def test_warn_once_inside_async_atomic(self):
        calls = self._connect("policy_warn_receiver")
        with self.assertWarns(SyncReceiverWarning) as cm:
            async with async_atomic():
                await TestModel.async_objects.acreate(name="w1", value=1)
                await TestModel.async_objects.acreate(name="w2", value=1)
        self.assertIn("policy_warn_receiver", str(cm.warning))
        self.assertIn("outside the async transaction", str(cm.warning))
        # The receiver still ran (in a thread), both times.
        self.assertEqual(calls, ["w1", "w2"])

    @override_settings(ASYNC_BACKEND_SYNC_RECEIVERS="error")
    async def test_error_policy(self):
        self._connect("policy_error_receiver")
        with self.assertRaises(SyncReceiverInTransaction):
            async with async_atomic():
                await TestModel.async_objects.acreate(name="e1", value=1)

    @override_settings(ASYNC_BACKEND_SYNC_RECEIVERS="thread")
    async def test_thread_policy_is_quiet(self):
        import warnings

        calls = self._connect("policy_thread_receiver")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            async with async_atomic():
                await TestModel.async_objects.acreate(name="t1", value=1)
        self.assertFalse([w for w in caught if issubclass(w.category, SyncReceiverWarning)])
        self.assertEqual(calls, ["t1"])

    async def test_outside_a_transaction_is_quiet(self):
        import warnings

        self._connect("policy_outside_receiver")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            await TestModel.async_objects.acreate(name="o1", value=1)
        self.assertFalse([w for w in caught if issubclass(w.category, SyncReceiverWarning)])


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
