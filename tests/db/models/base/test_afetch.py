from django.core.exceptions import SynchronousOnlyOperation
from django.db import DEFAULT_DB_ALIAS
from test_app.models import (
    M2MOwnerModel,
    M2MTagModel,
    SelectRelatedAuthorModel,
    SelectRelatedBookModel,
    SelectRelatedProfileModel,
    SelectRelatedPublisherModel,
    TestModel,
)

from django_async_backend.db import async_connections
from django_async_backend.test import (
    AsyncCaptureQueriesContext,
    AsyncioTestCase,
)


class AfetchTests(AsyncioTestCase):
    async def asyncSetUp(self):
        self.author = await SelectRelatedAuthorModel.async_objects.acreate(
            name="author"
        )
        self.publisher = (
            await SelectRelatedPublisherModel.async_objects.acreate(name="pub")
        )
        self.book = await SelectRelatedBookModel.async_objects.acreate(
            title="book", author=self.author, publisher=self.publisher
        )
        self.orphan = await SelectRelatedBookModel.async_objects.acreate(
            title="orphan", author=self.author
        )

    def _capture(self):
        return AsyncCaptureQueriesContext(async_connections[DEFAULT_DB_ALIAS])

    async def test_requires_a_name(self):
        with self.assertRaises(TypeError):
            await self.book.afetch()

    async def test_unknown_attribute(self):
        with self.assertRaises(AttributeError):
            await self.book.afetch("nope")

    async def test_loaded_field_is_reloaded_from_the_database(self):
        # Every concrete field is a DeferredAttribute, so afetch() on a
        # loaded one is a one-field refresh.
        await SelectRelatedBookModel.async_objects.filter(
            pk=self.book.pk
        ).aupdate(title="renamed")
        self.assertEqual(await self.book.afetch("title"), "renamed")
        self.assertEqual(self.book.title, "renamed")

    async def test_forward_fk_is_loaded_and_cached(self):
        book = await SelectRelatedBookModel.async_objects.aget(pk=self.book.pk)
        with self.assertRaises(SynchronousOnlyOperation):
            book.author

        async with self._capture() as ctx:
            author = await book.afetch("author")
            cached = book.author

        self.assertEqual(author.pk, self.author.pk)
        self.assertIs(cached, author)
        self.assertEqual(len(ctx), 1)

    async def test_null_fk_resolves_to_none_without_a_query(self):
        book = await SelectRelatedBookModel.async_objects.aget(
            pk=self.orphan.pk
        )
        async with self._capture() as ctx:
            self.assertIsNone(await book.afetch("publisher"))
            self.assertIsNone(book.publisher)
        self.assertEqual(len(ctx), 0)

    async def test_several_names_return_a_list(self):
        book = await SelectRelatedBookModel.async_objects.aget(pk=self.book.pk)
        author, publisher = await book.afetch("author", "publisher")
        self.assertEqual(
            (author.pk, publisher.pk), (self.author.pk, self.publisher.pk)
        )

    async def test_reverse_one_to_one(self):
        profile = await SelectRelatedProfileModel.async_objects.acreate(
            author=self.author, bio="bio"
        )
        author = await SelectRelatedAuthorModel.async_objects.aget(
            pk=self.author.pk
        )
        loaded = await author.afetch("profile")
        self.assertEqual(loaded.pk, profile.pk)
        self.assertIs(author.profile, loaded)
        # Both directions are cached.
        self.assertIs(loaded.author, author)

    async def test_reverse_one_to_one_missing_is_none(self):
        author = await SelectRelatedAuthorModel.async_objects.aget(
            pk=self.author.pk
        )
        self.assertIsNone(await author.afetch("profile"))

    async def test_deferred_field(self):
        book = await SelectRelatedBookModel.async_objects.only("title").aget(
            pk=self.book.pk
        )
        self.assertIn("author_id", book.get_deferred_fields())
        with self.assertRaises(SynchronousOnlyOperation):
            book.author_id

        async with self._capture() as ctx:
            value = await book.afetch("author_id")

        self.assertEqual(value, self.author.pk)
        self.assertEqual(book.author_id, self.author.pk)
        self.assertNotIn("author_id", book.get_deferred_fields())
        self.assertEqual(len(ctx), 1)

    async def test_reverse_fk_and_m2m_are_prefetched(self):
        owner = await M2MOwnerModel.async_objects.acreate(name="owner")
        tag = await M2MTagModel.async_objects.acreate(name="tag")
        await owner.tags.async_objects.aadd(tag)
        author = await SelectRelatedAuthorModel.async_objects.aget(
            pk=self.author.pk
        )

        books = await author.afetch("books")
        self.assertEqual(sorted(b.title for b in books), ["book", "orphan"])
        async with self._capture() as ctx:
            self.assertEqual(len(author.books.all()), 2)  # served from cache
        self.assertEqual(len(ctx), 0)

        owner = await M2MOwnerModel.async_objects.aget(pk=owner.pk)
        tags = await owner.afetch("tags")
        self.assertEqual([t.name for t in tags], ["tag"])

    async def test_self_referential_fk(self):
        parent = await TestModel.async_objects.acreate(name="parent", value=1)
        child = await TestModel.async_objects.acreate(
            name="child", value=2, relative=parent
        )
        child = await TestModel.async_objects.aget(pk=child.pk)
        self.assertEqual((await child.afetch("relative")).pk, parent.pk)
