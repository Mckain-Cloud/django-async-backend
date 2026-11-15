import asyncio
from unittest import mock

from asgiref.sync import sync_to_async
from django import forms
from django.core.exceptions import (
    SynchronousOnlyOperation,
    ValidationError,
)
from django.core.paginator import Paginator
from django.db import (
    connections,
    transaction,
)
from django.db.models import Count
from django.db.models.signals import (
    m2m_changed,
    post_delete,
    post_save,
)
from django.template import (
    Context,
    Template,
)
from test_app.models import (
    M2MOwnerModel,
    M2MTagModel,
    SelectRelatedAuthorModel,
    SelectRelatedBookModel,
    SelectRelatedPublisherModel,
    TestModel,
    TotalOrderingModel,
)

from django_async_backend.db import async_connections
from django_async_backend.db import blocking as db_blocking
from django_async_backend.db.blocking import BlockingQueryError
from django_async_backend.db.models import blocking
from django_async_backend.db.models import query as async_query
from django_async_backend.db.models.fetch_modes import FETCH_BLOCKING
from django_async_backend.db.models.sync_only import SyncOnlyError
from django_async_backend.db.transaction import async_atomic
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


class SyncWriteBlockingTests(_Installed, AsyncioTestCase):
    async def asyncSetUp(self):
        db_blocking.reset_reported()

    async def test_save_a_new_instance(self):
        obj = TestModel(name="new")
        obj.save()
        self.assertIsNotNone(obj.pk)
        self.assertFalse(obj._state.adding)
        self.assertEqual(await TestModel.async_objects.aget(pk=obj.pk), obj)

    async def test_save_update_fields(self):
        obj = await TestModel.async_objects.acreate(name="old")
        obj.name = "new"
        async with self.assertNumQueries(1):
            obj.save(update_fields=["name"])
        self.assertTrue(
            await TestModel.async_objects.filter(name="new").aexists()
        )

    async def test_save_runs_djangos_body_not_an_async_override(self):
        obj = TestModel(name="new")
        with mock.patch.object(
            TestModel, "async_save", side_effect=AssertionError, create=True
        ):
            obj.save()
        self.assertIsNotNone(obj.pk)

    async def test_delete_cascades(self):
        root = await TestModel.async_objects.acreate(name="root")
        await TestModel.async_objects.acreate(name="child", relative=root)
        deleted, per_model = root.delete()
        self.assertEqual(deleted, 2)
        self.assertEqual(per_model, {"test_app.TestModel": 2})
        self.assertEqual(await TestModel.async_objects.acount(), 0)

    async def test_sync_receivers_run_inline_in_the_transaction(self):
        seen = []

        def on_save(sender, instance, created, **kwargs):
            visible = (
                TestModel.objects.fetch_mode(FETCH_BLOCKING)
                .filter(pk=instance.pk)
                .exists()
            )
            seen.append(("save", created, visible))

        def on_delete(sender, instance, **kwargs):
            seen.append(("delete", instance.name))

        async def async_on_save(sender, instance, **kwargs):
            seen.append(("async", await TestModel.async_objects.acount()))

        post_save.connect(on_save, sender=TestModel)
        post_save.connect(async_on_save, sender=TestModel)
        post_delete.connect(on_delete, sender=TestModel)
        try:
            obj = TestModel(name="new")
            obj.save()
            obj.delete()
        finally:
            post_save.disconnect(on_save, sender=TestModel)
            post_save.disconnect(async_on_save, sender=TestModel)
            post_delete.disconnect(on_delete, sender=TestModel)
        self.assertEqual(
            seen, [("save", True, True), ("async", 1), ("delete", "new")]
        )

    async def test_receiver_that_awaits_other_io_is_refused(self):
        async def slow(sender, **kwargs):
            await asyncio.sleep(0.01)

        post_save.connect(slow, sender=TestModel)
        try:
            with self.assertRaises(BlockingQueryError):
                with transaction.atomic():
                    TestModel(name="new").save()
        finally:
            post_save.disconnect(slow, sender=TestModel)
        self.assertEqual(await TestModel.async_objects.acount(), 0)

    async def test_atomic_rolls_back(self):
        with self.assertRaisesRegex(ValueError, "boom"):
            with transaction.atomic():
                TestModel(name="gone").save()
                raise ValueError("boom")
        self.assertEqual(await TestModel.async_objects.acount(), 0)

    async def test_atomic_nests_with_async_atomic_and_decorator(self):
        @transaction.atomic
        def create(name, fail=False):
            TestModel(name=name).save()
            if fail:
                raise ValueError(name)

        connection = async_connections["default"]
        depth = len(connection.savepoint_ids)
        async with async_atomic():
            with transaction.atomic():
                create("kept")
                with self.assertRaises(ValueError):
                    create("dropped", fail=True)
                self.assertEqual(len(connection.savepoint_ids), depth + 2)
        self.assertEqual(len(connection.savepoint_ids), depth)
        self.assertEqual(
            [o.name async for o in TestModel.async_objects.all()], ["kept"]
        )

    async def test_on_commit_registers_on_the_async_connection(self):
        fired = []
        async with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with transaction.atomic():
                transaction.on_commit(lambda: fired.append(1))
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(fired, [1])

    async def test_many_to_many_writes(self):
        owner = await M2MOwnerModel.async_objects.acreate(name="owner")
        tags = [
            await M2MTagModel.async_objects.acreate(name="tag-%d" % i)
            for i in range(3)
        ]
        events = []

        def on_change(sender, action, **kwargs):
            events.append(action)

        m2m_changed.connect(on_change, sender=M2MOwnerModel.tags.through)
        try:
            owner.tags.add(tags[0], tags[1])
        finally:
            m2m_changed.disconnect(
                on_change, sender=M2MOwnerModel.tags.through
            )
        self.assertEqual(events, ["pre_add", "post_add"])
        self.assertEqual(await owner.tags.async_objects.acount(), 2)
        owner.tags.remove(tags[0])
        self.assertEqual(await owner.tags.async_objects.acount(), 1)
        owner.tags.set(tags)
        self.assertEqual(await owner.tags.async_objects.acount(), 3)
        tags[0].owners.clear()
        self.assertEqual(await owner.tags.async_objects.acount(), 2)
        created = owner.tags.create(name="made")
        self.assertEqual(created.name, "made")
        self.assertEqual(await owner.tags.async_objects.acount(), 3)

    async def test_many_to_many_count_and_exists(self):
        owner = await M2MOwnerModel.async_objects.acreate(name="owner")
        tag = await M2MTagModel.async_objects.acreate(name="tag")
        opted_in = await M2MOwnerModel.async_objects.fetch_mode(
            FETCH_BLOCKING
        ).aget(pk=owner.pk)
        self.assertFalse(opted_in.tags.exists())
        opted_in.tags.add(tag)
        self.assertEqual(opted_in.tags.count(), 1)
        self.assertTrue(opted_in.tags.exists())
        self.assertEqual([t.name for t in opted_in.tags.all()], ["tag"])
        # Reads still need the mode.
        with self.assertRaises(SynchronousOnlyOperation):
            owner.tags.count()

    async def test_reverse_foreign_key_writes(self):
        root = await TestModel.async_objects.acreate(name="root")
        a = await TestModel.async_objects.acreate(name="a")
        b = await TestModel.async_objects.acreate(name="b")
        root.relatives.add(a, b)
        self.assertEqual(await root.relatives.async_objects.acount(), 2)
        root.relatives.remove(a)
        self.assertEqual(await root.relatives.async_objects.acount(), 1)
        root.relatives.set([a])
        self.assertEqual(
            [o.name async for o in root.relatives.async_objects.all()], ["a"]
        )
        root.relatives.clear()
        self.assertEqual(await root.relatives.async_objects.acount(), 0)


class SyncWriteCommitTests(_Installed, AsyncioTransactionTestCase):
    async def asyncTearDown(self):
        await sync_to_async(connections.close_all)()

    async def test_first_use_in_a_task_needs_an_open_connection(self):
        with self.assertRaisesRegex(BlockingQueryError, "ensure_connection"):
            TestModel(name="new").save()
        await async_connections["default"].ensure_connection()
        TestModel(name="new").save()
        self.assertEqual(await TestModel.async_objects.acount(), 1)

    async def test_atomic_commits_and_runs_on_commit(self):
        await async_connections["default"].ensure_connection()
        fired = []
        with transaction.atomic():
            TestModel(name="kept").save()
            transaction.on_commit(lambda: fired.append("committed"))
            self.assertEqual(fired, [])
        self.assertEqual(fired, ["committed"])
        with self.assertRaises(ValueError):
            with transaction.atomic():
                TestModel(name="dropped").save()
                raise ValueError
        # A different connection sees exactly the committed row.
        names = await sync_to_async(
            lambda: list(TestModel.objects.values_list("name", flat=True))
        )()
        self.assertEqual(names, ["kept"])

    async def test_worker_thread_writes_use_the_sync_connection(self):
        def work():
            with transaction.atomic():
                obj = TestModel(name="threaded")
                obj.save()
            return obj.pk

        with self.assertNoLogs("django_async_backend.blocking", "WARNING"):
            pk = await sync_to_async(work)()
        self.assertTrue(await TestModel.async_objects.filter(pk=pk).aexists())


class BookForm(forms.ModelForm):
    class Meta:
        model = SelectRelatedBookModel
        fields = ["title", "author", "publisher"]


class OwnerForm(forms.ModelForm):
    class Meta:
        model = M2MOwnerModel
        fields = ["name", "tags"]


class SyncModelValidationTests(_Installed, AsyncioTestCase):
    async def asyncSetUp(self):
        db_blocking.reset_reported()

    async def test_refresh_from_db(self):
        obj = await TestModel.async_objects.acreate(name="old")
        await TestModel.async_objects.filter(pk=obj.pk).aupdate(name="new")
        async with self.assertNumQueries(1):
            obj.refresh_from_db()
        self.assertEqual(obj.name, "new")
        await TestModel.async_objects.filter(pk=obj.pk).aupdate(name="newer")
        obj.refresh_from_db(fields=["name"])
        self.assertEqual(obj.name, "newer")

    async def test_lazy_load_without_the_mode_still_raises(self):
        created = await TestModel.async_objects.acreate(name="row")
        obj = await TestModel.async_objects.defer("name").aget(pk=created.pk)
        with self.assertRaisesRegex(SynchronousOnlyOperation, "afetch"):
            obj.name
        obj.refresh_from_db()
        self.assertEqual(obj.name, "row")

    async def test_full_clean_checks_foreign_keys_and_uniqueness(self):
        await TestModel.async_objects.acreate(name="taken")
        with self.assertRaises(ValidationError) as cm:
            TestModel(name="taken", value=1, relative_id=10**9).full_clean()
        self.assertEqual(set(cm.exception.message_dict), {"name", "relative"})
        TestModel(name="free", value=1).full_clean()

    async def test_validate_constraints(self):
        await TotalOrderingModel.async_objects.acreate(
            headline="a", barcode="same"
        )
        candidate = TotalOrderingModel(headline="b", barcode="same")
        with self.assertRaises(ValidationError):
            candidate.validate_constraints()
        TotalOrderingModel(
            headline="b", barcode="other"
        ).validate_constraints()

    async def test_iterator_returns_the_rows(self):
        for name in ("a", "b", "c"):
            await TestModel.async_objects.acreate(name=name)
        sync_rows = TestModel.objects.fetch_mode(FETCH_BLOCKING).order_by(
            "name"
        )
        async_rows = TestModel.async_objects.fetch_mode(
            FETCH_BLOCKING
        ).order_by("name")
        async with self.assertNumQueries(2):
            self.assertEqual(
                [o.name for o in sync_rows.iterator(chunk_size=1)],
                ["a", "b", "c"],
            )
            self.assertEqual(
                [o.name for o in async_rows.iterator()], ["a", "b", "c"]
            )
        with self.assertRaises(SynchronousOnlyOperation):
            list(TestModel.objects.iterator())


class SyncModelFormTests(_Installed, AsyncioTestCase):
    async def asyncSetUp(self):
        db_blocking.reset_reported()
        self.author = await SelectRelatedAuthorModel.async_objects.acreate(
            name="Ann"
        )
        self.publisher = (
            await SelectRelatedPublisherModel.async_objects.acreate(name="Pub")
        )

    async def test_render_lists_the_choices(self):
        html = BookForm().as_p()
        self.assertIn('<option value="%d">' % self.author.pk, html)
        self.assertIn('<option value="%d">' % self.publisher.pk, html)

    async def test_valid_form_saves(self):
        form = BookForm(
            {
                "title": "Book",
                "author": str(self.author.pk),
                "publisher": str(self.publisher.pk),
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        book = form.save()
        saved = await SelectRelatedBookModel.async_objects.select_related(
            "author", "publisher"
        ).aget(pk=book.pk)
        self.assertEqual(
            (saved.title, saved.author.name, saved.publisher.name),
            ("Book", "Ann", "Pub"),
        )

    async def test_invalid_choice_and_unique_errors(self):
        await SelectRelatedBookModel.async_objects.acreate(
            title="Taken", author=self.author
        )
        form = BookForm({"title": "Taken", "author": "999999999"})
        self.assertFalse(form.is_valid())
        self.assertIn("valid choice", str(form.errors["author"]))
        form = BookForm({"title": "Taken", "author": str(self.author.pk)})
        self.assertFalse(form.is_valid())
        self.assertIn("already exists", str(form.errors["title"]))

    async def test_edit_form_with_many_to_many(self):
        tags = [
            await M2MTagModel.async_objects.acreate(name="tag-%d" % i)
            for i in range(3)
        ]
        owner = await M2MOwnerModel.async_objects.acreate(name="owner")
        await owner.tags.async_objects.aadd(tags[0])

        form = OwnerForm(instance=owner)
        self.assertEqual(form.initial["tags"], [tags[0]])
        self.assertIn("selected", form.as_p())

        form = OwnerForm(
            {"name": "renamed", "tags": [str(tags[1].pk), str(tags[2].pk)]},
            instance=owner,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(
            (await M2MOwnerModel.async_objects.aget(pk=owner.pk)).name,
            "renamed",
        )
        self.assertEqual(
            [t.name async for t in owner.tags.async_objects.order_by("name")],
            ["tag-1", "tag-2"],
        )

    async def test_form_in_a_sync_atomic_block_rolls_back(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                form = BookForm(
                    {"title": "Book", "author": str(self.author.pk)}
                )
                self.assertTrue(form.is_valid(), form.errors)
                form.save()
                raise RuntimeError
        self.assertEqual(
            await SelectRelatedBookModel.async_objects.acount(), 0
        )


class NotInstalledTests(AsyncioTestCase):
    async def test_writes_still_raise(self):
        owner = await M2MOwnerModel.async_objects.acreate(name="owner")
        tag = await M2MTagModel.async_objects.acreate(name="tag")
        with self.assertRaises(SynchronousOnlyOperation):
            TestModel(name="new").save()
        with self.assertRaises(SynchronousOnlyOperation):
            owner.delete()
        with self.assertRaises(SynchronousOnlyOperation):
            owner.tags.add(tag)
        with self.assertRaises(SynchronousOnlyOperation):
            with transaction.atomic():
                pass
        with self.assertRaises(SynchronousOnlyOperation):
            transaction.on_commit(lambda: None)
        with self.assertRaises(SynchronousOnlyOperation):
            owner.refresh_from_db()
        with self.assertRaises(SynchronousOnlyOperation):
            TestModel(name="owner").full_clean()
        with self.assertRaises(SynchronousOnlyOperation):
            BookForm().as_p()
