from asgiref.sync import sync_to_async
from django.db import (
    DEFAULT_DB_ALIAS,
    connections,
)
from django.db.models.signals import m2m_changed
from test_app.models import (
    M2MOwnerModel,
    M2MTagModel,
    SelectRelatedAuthorModel,
    SelectRelatedBookModel,
    SelectRelatedPublisherModel,
)

from django_async_backend.db import async_connections
from django_async_backend.db.models.related import (
    create_async_many_to_many_manager,
    create_async_reverse_many_to_one_manager,
)
from django_async_backend.db.transaction import async_atomic
from django_async_backend.test import (
    AsyncCaptureQueriesContext,
    AsyncioTestCase,
)


async def _close_sync_connections():
    # Tests that compare against the thread-backed sync ORM open a sync
    # connection in the worker thread; close it there, or the test database
    # cannot be dropped at the end of the run.
    await sync_to_async(connections.close_all)()


class ReverseManyToOneReadTests(AsyncioTestCase):
    async def asyncSetUp(self):
        self.author = await SelectRelatedAuthorModel.async_objects.acreate(
            name="author"
        )
        self.other = await SelectRelatedAuthorModel.async_objects.acreate(
            name="other"
        )
        self.b1 = await SelectRelatedBookModel.async_objects.acreate(
            title="b1", author=self.author
        )
        self.b2 = await SelectRelatedBookModel.async_objects.acreate(
            title="b2", author=self.author
        )
        await SelectRelatedBookModel.async_objects.acreate(
            title="b3", author=self.other
        )

    async def test_manager_class_is_cached_per_relation(self):
        rel = SelectRelatedBookModel._meta.get_field("author").remote_field
        self.assertIs(
            create_async_reverse_many_to_one_manager(rel),
            create_async_reverse_many_to_one_manager(rel),
        )
        self.assertIs(
            type(self.author.books.async_objects),
            create_async_reverse_many_to_one_manager(rel),
        )

    async def test_all_is_filtered_to_the_instance(self):
        titles = sorted(
            [b.title async for b in self.author.books.async_objects.all()]
        )
        self.assertEqual(titles, ["b1", "b2"])

    async def test_queryset_methods_are_available(self):
        manager = self.author.books.async_objects
        self.assertEqual(await manager.acount(), 2)
        self.assertTrue(await manager.filter(title="b1").aexists())
        self.assertEqual(
            (await manager.order_by("-title").afirst()).title, "b2"
        )
        self.assertEqual(
            sorted([t async for t in manager.values_list("title", flat=True)]),
            ["b1", "b2"],
        )

    async def test_known_related_object_is_reused(self):
        async with AsyncCaptureQueriesContext(
            async_connections[DEFAULT_DB_ALIAS]
        ) as ctx:
            book = await self.author.books.async_objects.afirst()
            author = book.author

        self.assertIs(author, self.author)
        self.assertEqual(len(ctx), 1)

    async def test_using_is_passed_through(self):
        manager = self.author.books.db_manager("other").async_objects
        self.assertEqual(manager._db, "other")

    async def test_selecting_another_manager_is_rejected(self):
        with self.assertRaises(TypeError):
            self.author.books.async_objects(manager="objects")


async def _close_sync_connections():
    # Tests that compare against the thread-backed sync ORM open a sync
    # connection in the worker thread; close it there, or the test database
    # cannot be dropped at the end of the run.
    await sync_to_async(connections.close_all)()


class ReverseManyToOneWriteTests(AsyncioTestCase):
    async def asyncSetUp(self):
        self.author = await SelectRelatedAuthorModel.async_objects.acreate(
            name="author"
        )
        self.other = await SelectRelatedAuthorModel.async_objects.acreate(
            name="other"
        )
        self.publisher = (
            await SelectRelatedPublisherModel.async_objects.acreate(name="pub")
        )

    async def _titles(self, manager):
        return sorted(
            [t async for t in manager.values_list("title", flat=True)]
        )

    async def test_acreate_sets_the_fk(self):
        book = await self.author.books.async_objects.acreate(title="new")
        self.assertEqual(book.author_id, self.author.pk)
        self.assertEqual(await self.author.books.async_objects.acount(), 1)

    async def test_aadd_bulk_updates_saved_objects(self):
        book = await SelectRelatedBookModel.async_objects.acreate(
            title="moved", author=self.other
        )
        await self.author.books.async_objects.aadd(book)
        await book.async_refresh_from_db()
        self.assertEqual(book.author_id, self.author.pk)

    async def test_aadd_bulk_rejects_unsaved(self):
        with self.assertRaises(ValueError):
            await self.author.books.async_objects.aadd(
                SelectRelatedBookModel(title="unsaved")
            )

    async def test_aadd_non_bulk_saves_each(self):
        book = SelectRelatedBookModel(title="unsaved")
        await self.author.books.async_objects.aadd(book, bulk=False)
        self.assertIsNotNone(book.pk)
        self.assertEqual(
            await self._titles(self.author.books.async_objects), ["unsaved"]
        )

    async def test_aadd_rejects_wrong_model(self):
        with self.assertRaises(TypeError):
            await self.author.books.async_objects.aadd(self.other)

    async def test_aget_or_create_and_aupdate_or_create(self):
        book, created = await self.author.books.async_objects.aget_or_create(
            title="goc"
        )
        self.assertTrue(created)
        self.assertEqual(book.author_id, self.author.pk)
        book2, created = (
            await self.author.books.async_objects.aupdate_or_create(
                title="goc", defaults={"publisher": self.publisher}
            )
        )
        self.assertFalse(created)
        self.assertEqual(book2.pk, book.pk)
        self.assertEqual(book2.publisher_id, self.publisher.pk)

    async def test_non_nullable_fk_has_no_remove_or_clear(self):
        manager = self.author.books.async_objects
        self.assertFalse(hasattr(manager, "aremove"))
        self.assertFalse(hasattr(manager, "aclear"))

    async def test_nullable_fk_aremove_aclear_aset(self):
        b1 = await SelectRelatedBookModel.async_objects.acreate(
            title="b1", author=self.author, publisher=self.publisher
        )
        b2 = await SelectRelatedBookModel.async_objects.acreate(
            title="b2", author=self.author, publisher=self.publisher
        )
        b3 = await SelectRelatedBookModel.async_objects.acreate(
            title="b3", author=self.author
        )
        manager = self.publisher.books.async_objects

        await manager.aremove(b1)
        self.assertEqual(await self._titles(manager), ["b2"])

        await manager.aset([b2, b3])
        self.assertEqual(await self._titles(manager), ["b2", "b3"])

        await manager.aset([b1], clear=True)
        self.assertEqual(await self._titles(manager), ["b1"])

        await manager.aclear(bulk=False)
        self.assertEqual(await self._titles(manager), [])
        await b1.async_refresh_from_db()
        self.assertIsNone(b1.publisher_id)

    async def test_aremove_of_unrelated_object_raises(self):
        book = await SelectRelatedBookModel.async_objects.acreate(
            title="b", author=self.author
        )
        with self.assertRaises(SelectRelatedPublisherModel.DoesNotExist):
            await self.publisher.books.async_objects.aremove(book)

    async def test_writes_join_the_surrounding_transaction(self):
        async with async_atomic():
            await self.author.books.async_objects.acreate(title="tx")
            inside = await self.author.books.async_objects.acount()
            self.assertEqual(inside, 1)
            # The sync ORM runs on another connection: nothing visible there.
            self.assertEqual(
                await SelectRelatedBookModel.objects.filter(
                    author=self.author
                ).acount(),
                0,
            )
            await _close_sync_connections()


async def _close_sync_connections():
    # Tests that compare against the thread-backed sync ORM open a sync
    # connection in the worker thread; close it there, or the test database
    # cannot be dropped at the end of the run.
    await sync_to_async(connections.close_all)()


class ManyToManyManagerTests(AsyncioTestCase):
    async def asyncSetUp(self):
        self.owner = await M2MOwnerModel.async_objects.acreate(name="owner")
        self.t1 = await M2MTagModel.async_objects.acreate(name="t1")
        self.t2 = await M2MTagModel.async_objects.acreate(name="t2")
        self.t3 = await M2MTagModel.async_objects.acreate(name="t3")

    async def _names(self, manager):
        return sorted(
            [n async for n in manager.values_list("name", flat=True)]
        )

    async def test_manager_class_is_cached(self):
        rel = M2MOwnerModel._meta.get_field("tags").remote_field
        self.assertIs(
            create_async_many_to_many_manager(rel, reverse=False),
            type(self.owner.tags.async_objects),
        )
        self.assertIs(
            create_async_many_to_many_manager(rel, reverse=True),
            type(self.t1.owners.async_objects),
        )

    async def test_aadd_aremove_aclear(self):
        manager = self.owner.tags.async_objects
        await manager.aadd(self.t1, self.t2)
        self.assertEqual(await self._names(manager), ["t1", "t2"])
        # Adding again is a no-op, and pks are accepted.
        await manager.aadd(self.t1, self.t3.pk)
        self.assertEqual(await self._names(manager), ["t1", "t2", "t3"])
        await manager.aremove(self.t2)
        self.assertEqual(await self._names(manager), ["t1", "t3"])
        await manager.aclear()
        self.assertEqual(await self._names(manager), [])

    async def test_reverse_side(self):
        await self.owner.tags.async_objects.aadd(self.t1)
        other = await M2MOwnerModel.async_objects.acreate(name="other")
        await self.t1.owners.async_objects.aadd(other)
        self.assertEqual(
            await self._names(self.t1.owners.async_objects), ["other", "owner"]
        )
        await self.t1.owners.async_objects.aremove(self.owner)
        self.assertEqual(
            await self._names(self.t1.owners.async_objects), ["other"]
        )

    async def test_aset(self):
        manager = self.owner.tags.async_objects
        await manager.aadd(self.t1, self.t2)
        await manager.aset([self.t2, self.t3])
        self.assertEqual(await self._names(manager), ["t2", "t3"])
        await manager.aset([self.t1], clear=True)
        self.assertEqual(await self._names(manager), ["t1"])

    async def test_acreate_and_aget_or_create(self):
        manager = self.owner.tags.async_objects
        tag = await manager.acreate(name="created")
        self.assertEqual(await self._names(manager), ["created"])
        same, created = await manager.aget_or_create(name="created")
        self.assertFalse(created)
        self.assertEqual(same.pk, tag.pk)
        _, created = await manager.aupdate_or_create(name="upserted")
        self.assertTrue(created)
        self.assertEqual(await self._names(manager), ["created", "upserted"])

    async def test_m2m_changed_is_sent_for_every_action(self):
        log = []

        def receiver(sender, action, pk_set, **kwargs):
            log.append((action, sorted(pk_set) if pk_set else pk_set))

        m2m_changed.connect(receiver, sender=M2MOwnerModel.tags.through)
        try:
            async with async_atomic():
                await self.owner.tags.async_objects.aadd(self.t1, self.t2)
                await self.owner.tags.async_objects.aremove(self.t1)
                await self.owner.tags.async_objects.aclear()
        finally:
            m2m_changed.disconnect(receiver, sender=M2MOwnerModel.tags.through)

        self.assertEqual(
            log,
            [
                ("pre_add", [self.t1.pk, self.t2.pk]),
                ("post_add", [self.t1.pk, self.t2.pk]),
                ("pre_remove", [self.t1.pk]),
                ("post_remove", [self.t1.pk]),
                ("pre_clear", None),
                ("post_clear", None),
            ],
        )

    async def test_async_receivers_cannot_use_the_connection(self):
        # Signal.asend() runs async receivers through asyncio.gather(), i.e.
        # in their own tasks, which do not own the task's connection. A
        # receiver that queries therefore fails loudly instead of silently
        # running on another connection.
        async def receiver(sender, **kwargs):
            await sender.async_objects.acount()

        m2m_changed.connect(receiver, sender=M2MOwnerModel.tags.through)
        try:
            with self.assertRaises(RuntimeError):
                await self.owner.tags.async_objects.aadd(self.t1)
        finally:
            m2m_changed.disconnect(receiver, sender=M2MOwnerModel.tags.through)

    async def test_prefetched_cache_is_dropped_on_write(self):
        await self.owner.tags.async_objects.aadd(self.t1)
        owner = await M2MOwnerModel.async_objects.prefetch_related(
            "tags"
        ).aget(pk=self.owner.pk)
        self.assertEqual([t.name for t in owner.tags.all()], ["t1"])
        await owner.tags.async_objects.aadd(self.t2)
        # The stale prefetch cache was removed; reading goes to the database.
        self.assertEqual(
            await self._names(owner.tags.async_objects), ["t1", "t2"]
        )

    async def test_through_rows_are_visible_in_the_same_transaction(self):
        async with async_atomic():
            await self.owner.tags.async_objects.aadd(self.t1)
            self.assertEqual(await self.owner.tags.async_objects.acount(), 1)
            self.assertEqual(await self.owner.tags.acount(), 0)  # thread hop
            await _close_sync_connections()
