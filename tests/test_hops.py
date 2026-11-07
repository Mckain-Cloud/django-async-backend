from asgiref.sync import sync_to_async
from test_app.models import TestModel

from django_async_backend import hops
from django_async_backend.test import AsyncioTransactionTestCase
from tests.contrib.test_admin import _close_sync_connections


def _sync_query():
    return list(TestModel.objects.all())


class SyncHopReporterTests(AsyncioTransactionTestCase):
    async def asyncTearDown(self):
        hops.uninstall()
        await _close_sync_connections()

    async def test_warns_once_per_call_site(self):
        hops.install("warn")
        with self.assertLogs("django_async_backend.hops", "WARNING") as cm:
            await sync_to_async(_sync_query)()
            await sync_to_async(_sync_query)()
        self.assertEqual(len(cm.output), 1)
        self.assertIn("sync ORM used in a worker thread", cm.output[0])
        self.assertIn("test_hops.py", cm.output[0])
        self.assertIn("_sync_query", cm.output[0])

    async def test_error_policy_raises_in_the_hop(self):
        hops.install("error")
        with self.assertRaises(hops.SyncHopError):
            await sync_to_async(_sync_query)()

    async def test_async_queries_are_not_reported(self):
        hops.install("error")
        self.assertEqual(await TestModel.async_objects.acount(), 0)
