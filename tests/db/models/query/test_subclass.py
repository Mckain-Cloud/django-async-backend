"""The async QuerySet is a real subclass of Django's, with the sync API
shadowed by loud errors."""

import pickle

from django.db.models import Manager
from django.db.models import QuerySet as DjangoQuerySet
from django.db.models.manager import BaseManager
from test_app.models import TestModel

from django_async_backend.db.models.query import QuerySet as AsyncQuerySet
from django_async_backend.db.models.sync_only import SYNC_ONLY, SyncOnlyError
from django_async_backend.test import AsyncioTestCase


class QuerySetSubclassTests(AsyncioTestCase):
    async def test_isinstance_checks_take_the_queryset_branch(self):
        qs = TestModel.async_objects.filter(name="x")
        self.assertIsInstance(qs, DjangoQuerySet)
        self.assertIsInstance(qs, AsyncQuerySet)
        self.assertIsInstance(TestModel.async_objects, Manager)
        self.assertIsInstance(TestModel.async_objects, BaseManager)
        self.assertNotIsInstance(TestModel.objects.all(), AsyncQuerySet)

    async def test_sync_methods_raise_with_the_async_hint(self):
        qs = TestModel.async_objects.all()
        for name, hint in SYNC_ONLY.items():
            with self.subTest(name=name):
                with self.assertRaises(SyncOnlyError) as cm:
                    getattr(qs, name)()
                self.assertIn(hint, str(cm.exception))
                self.assertIsInstance(cm.exception, TypeError)
        # Through the manager too.
        with self.assertRaises(SyncOnlyError):
            TestModel.async_objects.get(pk=1)
        with self.assertRaises(SyncOnlyError):
            TestModel.async_objects.count()

    async def test_sync_protocols_still_guarded(self):
        qs = TestModel.async_objects.all()
        with self.assertRaisesRegex(TypeError, "not iterable synchronously"):
            list(qs)
        with self.assertRaisesRegex(TypeError, "len\\(\\) is not supported"):
            len(qs)
        with self.assertRaises(TypeError):
            pickle.dumps(qs)
        self.assertEqual(repr(qs), "<QuerySet [%s]>" % qs.query)

    async def test_async_api_and_query_building_unaffected(self):
        await TestModel.async_objects.acreate(name="a", value=1)
        qs = TestModel.async_objects.filter(value__gte=1).order_by("name")
        self.assertEqual(await qs.acount(), 1)
        self.assertEqual([o.name async for o in qs], ["a"])
        self.assertEqual((await qs.aget()).name, "a")
        self.assertTrue(qs.ordered)
        self.assertEqual(qs.model, TestModel)
