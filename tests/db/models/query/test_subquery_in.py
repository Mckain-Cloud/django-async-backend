from django.db.models.sql.query import Query as DjangoQuery
from test_app.models import (
    SelectRelatedAuthorModel,
    SelectRelatedBookModel,
)

from django_async_backend.db.models import sql as async_sql
from django_async_backend.db.models.convert import (
    is_sync_queryset,
    to_async_queryset,
)
from django_async_backend.db.models.query import QuerySet as AsyncQuerySet
from django_async_backend.test import AsyncioTestCase


class SubqueryInTests(AsyncioTestCase):
    async def asyncSetUp(self):
        self.a1 = await SelectRelatedAuthorModel.async_objects.acreate(
            name="a1"
        )
        self.a2 = await SelectRelatedAuthorModel.async_objects.acreate(
            name="a2"
        )
        await SelectRelatedBookModel.async_objects.acreate(
            title="b1", author=self.a1
        )
        await SelectRelatedBookModel.async_objects.acreate(
            title="b2", author=self.a2
        )

    async def test_async_query_is_a_django_query(self):
        self.assertTrue(issubclass(async_sql.Query, DjangoQuery))

    async def test_in_lookup_with_a_full_async_queryset_is_narrowed_to_pk(
        self,
    ):
        authors = SelectRelatedAuthorModel.async_objects.filter(name="a1")
        titles = [
            t
            async for t in SelectRelatedBookModel.async_objects.filter(
                author__in=authors
            ).values_list("title", flat=True)
        ]
        self.assertEqual(titles, ["b1"])
        # Explicit values("pk") keeps working too.
        titles = [
            t
            async for t in SelectRelatedBookModel.async_objects.filter(
                author__in=authors.values("pk")
            ).values_list("title", flat=True)
        ]
        self.assertEqual(titles, ["b1"])


class ToAsyncQuerysetTests(AsyncioTestCase):
    async def asyncSetUp(self):
        self.a1 = await SelectRelatedAuthorModel.async_objects.acreate(
            name="a1"
        )
        await SelectRelatedBookModel.async_objects.acreate(
            title="b1", author=self.a1
        )
        await SelectRelatedBookModel.async_objects.acreate(
            title="b2", author=self.a1
        )

    async def test_conversion_keeps_the_query(self):
        sync_qs = (
            SelectRelatedBookModel.objects.filter(author__name="a1")
            .select_related("author")
            .order_by("-title")
        )
        self.assertTrue(is_sync_queryset(sync_qs))
        qs = to_async_queryset(sync_qs)
        self.assertIsInstance(qs, AsyncQuerySet)
        self.assertFalse(is_sync_queryset(qs))
        rows = [b async for b in qs]
        self.assertEqual([b.title for b in rows], ["b2", "b1"])
        self.assertIs(rows[0].author, rows[0].author)  # select_related kept
        self.assertEqual(rows[0].author.name, "a1")

    async def test_values_and_values_list_variants(self):
        qs = to_async_queryset(
            SelectRelatedBookModel.objects.order_by("title").values_list(
                "title", flat=True
            )
        )
        self.assertEqual([t async for t in qs], ["b1", "b2"])
        qs = to_async_queryset(
            SelectRelatedBookModel.objects.order_by("title").values("title")
        )
        self.assertEqual(
            [r async for r in qs], [{"title": "b1"}, {"title": "b2"}]
        )

    async def test_async_and_evaluated_querysets_pass_through(self):
        async_qs = SelectRelatedBookModel.async_objects.all()
        self.assertIs(to_async_queryset(async_qs), async_qs)
        evaluated = SelectRelatedBookModel.objects.none()
        list(evaluated)
        self.assertIs(to_async_queryset(evaluated), evaluated)
