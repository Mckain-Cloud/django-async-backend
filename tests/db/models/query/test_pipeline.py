import contextlib
from unittest import mock

import psycopg
from django.contrib.contenttypes.models import ContentType
from django.db import DEFAULT_DB_ALIAS
from django.db.models import Count
from django.test import override_settings
from test_app.models import (
    GenericFkModel,
    SelectRelatedAuthorModel,
    SelectRelatedBookModel,
    SelectRelatedProfileModel,
    SelectRelatedPublisherModel,
)

from django_async_backend.db import async_connections
from django_async_backend.db.models.pipeline import aevaluate_pipelined
from django_async_backend.db.transaction import async_atomic
from django_async_backend.test import (
    AsyncCaptureQueriesContext,
    AsyncioTestCase,
)
from django_async_backend.utils.contenttypes import aget_for_model


@contextlib.contextmanager
def count_pipelines():
    """Record every psycopg pipeline() block that is entered."""
    entered = []
    original = psycopg.AsyncConnection.pipeline

    def pipeline(self, *args, **kwargs):
        entered.append(self)
        return original(self, *args, **kwargs)

    with mock.patch.object(psycopg.AsyncConnection, "pipeline", pipeline):
        yield entered


class PipelineTestCase(AsyncioTestCase):
    async def asyncSetUp(self):
        # Pipelining is off by default on Windows; these tests need it on.
        self._pipeline_setting = override_settings(ASYNC_BACKEND_PIPELINE=True)
        self._pipeline_setting.enable()
        self.addCleanup(self._pipeline_setting.disable)
        self.author1 = await SelectRelatedAuthorModel.async_objects.acreate(
            name="Author1"
        )
        self.author2 = await SelectRelatedAuthorModel.async_objects.acreate(
            name="Author2"
        )
        await SelectRelatedProfileModel.async_objects.acreate(
            author=self.author1, bio="Bio1"
        )
        self.publisher = (
            await SelectRelatedPublisherModel.async_objects.acreate(
                name="Publisher1"
            )
        )
        self.book1 = await SelectRelatedBookModel.async_objects.acreate(
            title="Book1", author=self.author1, publisher=self.publisher
        )
        self.book2 = await SelectRelatedBookModel.async_objects.acreate(
            title="Book2", author=self.author2, publisher=None
        )

    def capture(self):
        return AsyncCaptureQueriesContext(async_connections[DEFAULT_DB_ALIAS])

    def querysets(self):
        books = SelectRelatedBookModel.async_objects
        return [
            books.select_related("publisher").order_by("title"),
            books.order_by("-title").values_list("title", flat=True),
            SelectRelatedAuthorModel.async_objects.annotate(
                n=Count("books")
            ).order_by("name"),
            books.filter(pk__in=[]),  # EmptyResultSet: no query at all
            books.extra(select={"shout": "upper(title)"}).order_by("pk"),
        ]


class TestAevaluatePipelined(PipelineTestCase):
    async def test_results_match_normal_evaluation(self):
        expected = []
        for qs in self.querysets():
            expected.append([row async for row in qs])

        batch = self.querysets()
        with count_pipelines() as entered:
            async with self.capture() as ctx:
                await aevaluate_pipelined(batch)

        self.assertEqual(len(entered), 1)
        # Four statements sent; the empty IN () sends none.
        self.assertEqual(len(ctx.captured_queries), 4)
        for qs, rows in zip(batch, expected):
            self.assertIsNotNone(qs._result_cache)
            self.assertEqual(qs._result_cache, rows)

        books = batch[0]._result_cache
        # select_related populated from the pipelined rows.
        self.assertEqual(books[0].publisher, self.publisher)
        self.assertIsNone(books[1].publisher)
        self.assertEqual([a.n for a in batch[2]._result_cache], [1, 1])
        self.assertEqual(batch[4]._result_cache[0].shout, "BOOK1")

    async def test_evaluated_querysets_are_reusable(self):
        batch = self.querysets()
        await aevaluate_pipelined(batch)
        for qs in batch:
            self.assertNotIn("get_compiler", vars(qs.query))
        # A clone compiles and runs its own query again.
        await SelectRelatedBookModel.async_objects.acreate(
            title="Book3", author=self.author1
        )
        self.assertEqual(await batch[0].all().acount(), 3)
        self.assertEqual(len(batch[0]._result_cache), 2)

    async def test_single_or_disabled_runs_without_pipeline(self):
        with count_pipelines() as entered:
            await aevaluate_pipelined(self.querysets()[:1])
            with override_settings(ASYNC_BACKEND_PIPELINE=False):
                batch = self.querysets()
                await aevaluate_pipelined(batch)
        self.assertEqual(entered, [])
        self.assertTrue(all(qs._result_cache is not None for qs in batch))

    async def test_already_evaluated_querysets_are_skipped(self):
        first, second, third = self.querysets()[:3]
        await first._fetch_all()
        with count_pipelines() as entered:
            async with self.capture() as ctx:
                await aevaluate_pipelined([first, second, third])
        self.assertEqual(len(entered), 1)
        self.assertEqual(len(ctx.captured_queries), 2)

    async def test_error_aborts_the_batch_and_the_atomic_block(self):
        bad = SelectRelatedBookModel.async_objects.extra(
            where=["1 / (id - id) = 1"]
        )
        good = SelectRelatedBookModel.async_objects.order_by("pk")
        with self.assertRaises(Exception) as cm:
            async with async_atomic():
                await SelectRelatedPublisherModel.async_objects.acreate(
                    name="Rolled back"
                )
                await aevaluate_pipelined([good, bad])
        self.assertIn("division by zero", str(cm.exception))
        self.assertIsNone(bad._result_cache)
        # The savepoint rolled back; the connection is usable.
        self.assertFalse(
            await SelectRelatedPublisherModel.async_objects.filter(
                name="Rolled back"
            ).aexists()
        )
        self.assertEqual(
            await SelectRelatedBookModel.async_objects.acount(), 2
        )


class TestPipelinedPrefetch(PipelineTestCase):
    async def test_sibling_lookups_share_one_round_trip(self):
        qs = SelectRelatedBookModel.async_objects.order_by(
            "title"
        ).prefetch_related("author", "publisher")
        with count_pipelines() as entered:
            async with self.capture() as ctx:
                books = [b async for b in qs]

        self.assertEqual(len(entered), 1)
        self.assertEqual(len(ctx.captured_queries), 3)
        self.assertEqual(
            [b.author for b in books], [self.author1, self.author2]
        )
        self.assertEqual([b.publisher for b in books], [self.publisher, None])

    async def test_reverse_and_nested_lookups(self):
        qs = SelectRelatedAuthorModel.async_objects.order_by(
            "name"
        ).prefetch_related("books__publisher", "profile")
        with count_pipelines() as entered:
            async with self.capture() as ctx:
                authors = [a async for a in qs]
                books = [list(a.books.all()) for a in authors]

        # Level 0 (books + profile) pipelined; books__publisher is level 1.
        self.assertEqual(len(entered), 1)
        self.assertEqual(len(ctx.captured_queries), 4)
        self.assertEqual(books, [[self.book1], [self.book2]])
        self.assertEqual(books[0][0].publisher, self.publisher)
        self.assertEqual(authors[0].profile.bio, "Bio1")

    async def test_single_lookup_is_not_pipelined(self):
        qs = SelectRelatedBookModel.async_objects.prefetch_related("author")
        with count_pipelines() as entered:
            [b async for b in qs]
        self.assertEqual(entered, [])

    async def test_generic_foreign_key_targets_share_one_round_trip(self):
        ContentType.objects.clear_cache()
        for target in (self.author1, self.publisher, self.book1):
            ct = await aget_for_model(type(target))
            await GenericFkModel.async_objects.acreate(
                name="g", content_type_id=ct.pk, object_id=target.pk
            )
        # Warm the ContentType cache so only the target queries remain.
        objects = [
            o async for o in GenericFkModel.async_objects.order_by("pk")
        ]

        with count_pipelines() as entered:
            async with self.capture() as ctx:
                prefetched = [
                    o
                    async for o in GenericFkModel.async_objects.order_by(
                        "pk"
                    ).prefetch_related("content_object")
                ]

        self.assertEqual(len(objects), 3)
        self.assertEqual(len(entered), 1)
        self.assertEqual(len(ctx.captured_queries), 4)
        self.assertEqual(
            [o.content_object for o in prefetched],
            [self.author1, self.publisher, self.book1],
        )
