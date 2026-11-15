from asgiref.sync import sync_to_async
from django.core.exceptions import SynchronousOnlyOperation
from django.core.paginator import Paginator
from django.db import connections
from django.db.models import Count
from django.template import (
    Context,
    Template,
)
from test_app.models import TestModel

from django_async_backend.db import blocking as db_blocking
from django_async_backend.db.models import blocking
from django_async_backend.db.models import query as async_query
from django_async_backend.db.models.fetch_modes import FETCH_BLOCKING
from django_async_backend.db.models.sync_only import SyncOnlyError
from django_async_backend.test import (
    AsyncioTestCase,
    AsyncioTransactionTestCase,
)

N = 12


class _Installed:
    @classmethod
    def setUpClass(cls):
        blocking.install()
        cls.addClassCleanup(blocking.uninstall)
        super().setUpClass()

    async def _create(self):
        db_blocking.reset_reported()
        self.root = await TestModel.async_objects.acreate(name="root")
        self.mid = await TestModel.async_objects.acreate(
            name="mid", relative=self.root
        )
        await TestModel.async_objects.abulk_create(
            [
                TestModel(name="row-%02d" % i, relative=self.mid)
                for i in range(N)
            ]
        )
        self.first = await TestModel.async_objects.aget(name="row-00")


class InstallTests(AsyncioTestCase):
    async def test_every_bridged_method_has_its_async_twin(self):
        for name, twin in blocking._TWINS.items():
            self.assertTrue(
                callable(getattr(async_query.QuerySet, twin, None)),
                "%s -> %s" % (name, twin),
            )

    async def test_install_and_uninstall_restore_the_classes(self):
        from django.db.models.query import QuerySet

        before = (
            QuerySet._fetch_all,
            QuerySet.count,
            async_query.QuerySet.__iter__,
            async_query.QuerySet.count,
        )
        blocking.install()
        blocking.install()
        self.assertIsNot(QuerySet._fetch_all, before[0])
        self.assertTrue(QuerySet.delete.alters_data)
        self.assertTrue(QuerySet.delete.queryset_only)
        blocking.uninstall()
        self.assertEqual(
            before,
            (
                QuerySet._fetch_all,
                QuerySet.count,
                async_query.QuerySet.__iter__,
                async_query.QuerySet.count,
            ),
        )
        self.assertNotIn("__contains__", vars(async_query.QuerySet))


class AsyncQuerySetBlockingTests(_Installed, AsyncioTestCase):
    async def asyncSetUp(self):
        await self._create()
        self.queryset = (
            TestModel.async_objects.filter(relative=self.mid)
            .order_by("name")
            .fetch_mode(FETCH_BLOCKING)
        )

    async def test_dunders(self):
        async with self.assertNumQueries(1):
            queryset = self.queryset.all()
            self.assertEqual(len(queryset), N)
            self.assertEqual([o.name for o in queryset][0], "row-00")
            self.assertTrue(queryset)
            self.assertIn(self.first, queryset)
        self.assertFalse(self.queryset.filter(name="nope"))

    async def test_methods(self):
        self.assertEqual(self.queryset.count(), N)
        self.assertTrue(self.queryset.exists())
        self.assertEqual(self.queryset.first().name, "row-00")
        self.assertEqual(self.queryset.last().name, "row-%02d" % (N - 1))
        self.assertEqual(self.queryset.get(name="row-00").pk, self.first.pk)
        self.assertEqual(self.queryset.aggregate(n=Count("pk")), {"n": N})
        self.assertTrue(self.queryset.contains(self.first))
        self.assertEqual(
            self.queryset.in_bulk([self.first.pk])[self.first.pk].name,
            "row-00",
        )

    async def test_writes(self):
        self.assertEqual(
            self.queryset.filter(name="row-00").update(name="renamed"), 1
        )
        self.assertEqual(self.queryset.filter(name="renamed").delete()[0], 1)
        created = self.queryset.create(name="created")
        self.assertEqual(await TestModel.async_objects.acount(), N + 2)
        self.assertIsNotNone(created.pk)

    async def test_async_api_is_unchanged(self):
        self.assertEqual(await self.queryset.acount(), N)
        self.assertEqual(len([o async for o in self.queryset]), N)
        self.assertEqual((await self.queryset[0]).name, "row-00")

    async def test_other_querysets_still_raise(self):
        queryset = TestModel.async_objects.all()
        with self.assertRaisesRegex(TypeError, "not iterable synchronously"):
            list(queryset)
        with self.assertRaisesRegex(TypeError, "len"):
            len(queryset)
        with self.assertRaisesRegex(TypeError, "truth-testing"):
            bool(queryset)
        with self.assertRaisesRegex(TypeError, r"await qs\.acontains\(obj\)"):
            self.first in queryset
        with self.assertRaises(SyncOnlyError):
            queryset.count()


class SyncQuerySetBlockingTests(_Installed, AsyncioTestCase):
    async def asyncSetUp(self):
        await self._create()
        self.obj = await TestModel.async_objects.fetch_mode(
            FETCH_BLOCKING
        ).aget(pk=self.mid.pk)
        self.manager = TestModel.objects.fetch_mode(FETCH_BLOCKING)

    async def test_reverse_manager_reads(self):
        related = self.obj.relatives
        self.assertEqual(len(list(related.all())), N)
        self.assertEqual(related.count(), N)
        self.assertTrue(related.exists())
        self.assertEqual(related.order_by("name").first().name, "row-00")
        self.assertEqual(related.order_by("name")[0].name, "row-00")
        self.assertEqual(
            [o.name for o in related.order_by("name")[2:4]],
            ["row-02", "row-03"],
        )
        self.assertIn(self.first, related.all())
        self.assertEqual(
            list(related.order_by("name").values_list("name", flat=True))[:2],
            ["row-00", "row-01"],
        )

    async def test_rows_from_a_manager_inherit_the_mode(self):
        child = self.obj.relatives.order_by("name")[0]
        self.assertEqual(child.relative.relative.name, "root")

    async def test_plain_manager_sees_the_transaction(self):
        self.assertEqual(self.manager.count(), N + 2)
        self.assertEqual(self.manager.get(name="root").pk, self.root.pk)

    async def test_select_related_and_prefetch_related(self):
        async with self.assertNumQueries(1):
            row = self.manager.select_related("relative").get(name="row-00")
            self.assertEqual(row.relative.name, "mid")
        async with self.assertNumQueries(2):
            mid = self.manager.prefetch_related("relatives").get(name="mid")
            self.assertEqual(len(mid.relatives.all()), N)

    async def test_sync_template(self):
        template = Template(
            "{% for r in o.relatives.all|slice:':2' %}{{ r.name }},"
            "{% endfor %}{{ o.relatives.count }}"
        )
        html = template.render(Context({"o": self.obj}))
        self.assertTrue(html.endswith(",%d" % N), html)
        self.assertEqual(html.count("row-"), 2)

    async def test_paginator(self):
        paginator = Paginator(self.obj.relatives.order_by("name"), 5)
        self.assertEqual(paginator.count, N)
        self.assertEqual(
            [o.name for o in paginator.page(2)][:2], ["row-05", "row-06"]
        )

    async def test_queryset_writes(self):
        created = self.obj.relatives.create(name="created")
        self.assertEqual(created.relative_id, self.obj.pk)
        self.assertEqual(
            self.manager.filter(name="created").update(name="renamed"), 1
        )
        self.assertEqual(self.manager.filter(name="renamed").delete()[0], 1)
        self.assertEqual(await TestModel.async_objects.acount(), N + 2)

    async def test_model_save_is_not_covered(self):
        with self.assertRaises(SynchronousOnlyOperation):
            self.obj.save()

    async def test_call_site_is_logged(self):
        with self.assertLogs("django_async_backend.blocking", "WARNING") as cm:
            self.obj.relatives.count()
        self.assertIn("test_call_site_is_logged", cm.output[0])

    async def test_other_querysets_still_raise(self):
        with self.assertRaises(SynchronousOnlyOperation):
            list(TestModel.objects.all())
        with self.assertRaises(SynchronousOnlyOperation):
            TestModel.objects.count()
        plain = await TestModel.async_objects.aget(pk=self.mid.pk)
        with self.assertRaises(SynchronousOnlyOperation):
            list(plain.relatives.all())


class SyncQuerySetThreadTests(_Installed, AsyncioTransactionTestCase):
    async def asyncTearDown(self):
        await sync_to_async(connections.close_all)()

    async def test_worker_thread_uses_the_sync_connection(self):
        await TestModel.async_objects.acreate(name="committed")
        manager = TestModel.objects.fetch_mode(FETCH_BLOCKING)
        with self.assertNoLogs("django_async_backend.blocking", "WARNING"):
            names = await sync_to_async(
                lambda: [o.name for o in manager.all()]
            )()
            count = await sync_to_async(manager.count)()
        self.assertEqual(names, ["committed"])
        self.assertEqual(count, 1)
