from django.core.exceptions import SynchronousOnlyOperation
from django.db import DEFAULT_DB_ALIAS
from test_app.models import TestModel

from django_async_backend.db import async_connections
from django_async_backend.test import (
    AsyncCaptureQueriesContext,
    AsyncioTestCase,
)


class OnlyDeferTests(AsyncioTestCase):
    async def asyncSetUp(self):
        self.obj = await TestModel.async_objects.acreate(name="n", value=7)

    async def _one(self, qs):
        async with AsyncCaptureQueriesContext(
            async_connections[DEFAULT_DB_ALIAS]
        ) as ctx:
            obj = await qs.aget(pk=self.obj.pk)
        return obj, ctx.captured_queries[0]["sql"]

    async def test_only_limits_the_select(self):
        obj, sql = await self._one(TestModel.async_objects.only("name"))
        self.assertNotIn('"value"', sql)
        self.assertEqual(obj.name, "n")
        self.assertEqual(
            sorted(obj.get_deferred_fields()), ["relative_id", "value"]
        )

    async def test_defer_limits_the_select(self):
        obj, sql = await self._one(TestModel.async_objects.defer("value"))
        self.assertNotIn('"value"', sql)
        self.assertEqual(obj.get_deferred_fields(), {"value"})

    async def test_deferred_access_raises_and_afetch_loads(self):
        obj, _ = await self._one(TestModel.async_objects.only("name"))
        with self.assertRaises(SynchronousOnlyOperation):
            obj.value
        self.assertEqual(await obj.afetch("value"), 7)
        self.assertEqual(obj.value, 7)

    async def test_manager_exposes_both(self):
        self.assertTrue(hasattr(TestModel.async_objects, "only"))
        self.assertTrue(hasattr(TestModel.async_objects, "defer"))

    async def test_private_alias_for_the_collector(self):
        qs = TestModel.async_objects.all()
        self.assertEqual(
            qs._only("name").query.deferred_loading,
            qs.only("name").query.deferred_loading,
        )

    async def test_async_save_of_deferred_instance_updates_loaded_fields(self):
        obj, _ = await self._one(TestModel.async_objects.only("name"))
        obj.name = "renamed"
        await obj.async_save()
        await self.obj.async_refresh_from_db()
        self.assertEqual((self.obj.name, self.obj.value), ("renamed", 7))
