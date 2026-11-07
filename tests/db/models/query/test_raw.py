from django.core.exceptions import FieldDoesNotExist
from django.db import DEFAULT_DB_ALIAS
from test_app.models import TestModel

from django_async_backend.db import async_connections
from django_async_backend.db.models.query import RawQuerySet
from django_async_backend.db.models.sql import RawQuery
from django_async_backend.test import (
    AsyncCaptureQueriesContext,
    AsyncioTestCase,
)

TABLE = TestModel._meta.db_table


class RawQuerySetTests(AsyncioTestCase):
    async def asyncSetUp(self):
        self.parent = await TestModel.async_objects.acreate(
            name="parent", value=1
        )
        self.child = await TestModel.async_objects.acreate(
            name="child", value=2, relative=self.parent
        )

    async def test_raw_returns_async_raw_queryset(self):
        qs = TestModel.async_objects.raw(f"SELECT * FROM {TABLE}")
        self.assertIsInstance(qs, RawQuerySet)
        self.assertIsInstance(qs.query, RawQuery)
        # araw() is the same thing, inherited from the patched sync QuerySet.
        self.assertIsInstance(TestModel.async_objects.araw(f"SELECT * FROM {TABLE}"), RawQuerySet)

    async def test_sync_manager_araw(self):
        """``Model.objects.araw()`` evaluates on the async connection."""
        qs = TestModel.objects.araw(
            f"SELECT * FROM {TABLE} WHERE value >= %s ORDER BY id", [1]
        )
        self.assertIsInstance(qs, RawQuerySet)
        self.assertIsInstance(qs.query, RawQuery)
        rows = [obj async for obj in qs]
        self.assertEqual([r.name for r in rows], ["parent", "child"])
        self.assertEqual(rows[1].relative_id, self.parent.pk)
        # Through a sync queryset and a related manager too.
        self.assertIsInstance(
            TestModel.objects.filter(value=1).araw(f"SELECT * FROM {TABLE}"), RawQuerySet
        )
        self.assertIsInstance(
            self.parent.relatives.araw(f"SELECT * FROM {TABLE}"), RawQuerySet
        )
        # The sync raw() is unchanged.
        self.assertNotIsInstance(TestModel.objects.raw(f"SELECT * FROM {TABLE}"), RawQuerySet)

    async def test_async_iteration(self):
        rows = [
            obj
            async for obj in TestModel.async_objects.raw(
                f"SELECT * FROM {TABLE} ORDER BY id"
            )
        ]
        self.assertEqual([r.name for r in rows], ["parent", "child"])
        self.assertEqual(rows[1].relative_id, self.parent.pk)

    async def test_params_and_annotations(self):
        rows = [
            obj
            async for obj in TestModel.async_objects.raw(
                f"SELECT id, name, value * 10 AS tenfold FROM {TABLE} "
                "WHERE name = %s",
                [self.child.name],
            )
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].tenfold, 20)
        # Columns not selected are deferred.
        self.assertIn("value", rows[0].get_deferred_fields())
        self.assertEqual(await rows[0].afetch("value"), 2)

    async def test_result_is_cached(self):
        qs = TestModel.async_objects.raw(f"SELECT * FROM {TABLE}")
        async with AsyncCaptureQueriesContext(
            async_connections[DEFAULT_DB_ALIAS]
        ) as ctx:
            first = [o async for o in qs]
            second = [o async for o in qs]
        self.assertEqual(len(first), len(second), 2)
        self.assertEqual(len(ctx), 1)

    async def test_aiterator_streams_without_caching(self):
        qs = TestModel.async_objects.raw(f"SELECT * FROM {TABLE} ORDER BY id")
        names = [o.name async for o in qs.aiterator()]
        self.assertEqual(names, ["parent", "child"])
        self.assertIsNone(qs._result_cache)

    async def test_missing_primary_key(self):
        with self.assertRaises(FieldDoesNotExist):
            [
                o
                async for o in TestModel.async_objects.raw(
                    f"SELECT name FROM {TABLE}"
                )
            ]

    async def test_sync_protocol_is_rejected(self):
        qs = TestModel.async_objects.raw(f"SELECT * FROM {TABLE}")
        with self.assertRaises(TypeError):
            list(qs)
        with self.assertRaises(TypeError):
            len(qs)
        with self.assertRaises(TypeError):
            bool(qs)
        with self.assertRaises(TypeError):
            qs.columns  # not executed yet

    async def test_cursor_is_closed_after_iteration(self):
        qs = TestModel.async_objects.raw(f"SELECT * FROM {TABLE}")
        [o async for o in qs]
        self.assertTrue(qs.query.cursor.closed)

    async def test_prefetch_related(self):
        rows = [
            o
            async for o in TestModel.async_objects.raw(
                f"SELECT * FROM {TABLE} ORDER BY id"
            ).prefetch_related("relatives")
        ]
        self.assertEqual([r.name for r in rows[0].relatives.all()], ["child"])

    async def test_using_other_alias(self):
        qs = TestModel.async_objects.raw(f"SELECT * FROM {TABLE}").using(
            "other"
        )
        self.assertEqual(qs.db, "other")
