"""django-filter through AsyncDjangoFilterBackend: validation and filtering
on the async connection."""

from django.test import AsyncClient
from test_app.models import (
    SelectRelatedAuthorModel,
    SelectRelatedBookModel,
    SelectRelatedPublisherModel,
)

from django_async_backend.test import AsyncioTransactionTestCase
from tests.contrib.test_admin import (
    _close_sync_connections,
    no_sync_queries,
)


class FilterBackendTests(AsyncioTransactionTestCase):
    async def asyncSetUp(self):
        self.client = AsyncClient()
        self.a1 = await SelectRelatedAuthorModel.async_objects.acreate(name="ann")
        self.a2 = await SelectRelatedAuthorModel.async_objects.acreate(name="bob")
        self.pub = await SelectRelatedPublisherModel.async_objects.acreate(name="p")
        for i in range(6):
            await SelectRelatedBookModel.async_objects.acreate(
                title="book-%02d" % i,
                author=self.a1 if i % 2 else self.a2,
                publisher=self.pub if i < 2 else None,
            )

    async def asyncTearDown(self):
        await _close_sync_connections()
        for model in (
            SelectRelatedBookModel,
            SelectRelatedPublisherModel,
            SelectRelatedAuthorModel,
        ):
            await model.async_objects.all().adelete()

    async def test_declared_filterset(self):
        base = "/api/filtered-books/"
        with no_sync_queries():
            r = await self.client.get(base)
            self.assertEqual(r.status_code, 200, r.content[:300])
            self.assertEqual(r.json()["count"], 6)
            r = await self.client.get(base + "?author=%s" % self.a1.pk)
            self.assertEqual(r.json()["count"], 3)
            self.assertTrue(all(b["author_name"] == "ann" for b in r.json()["results"]))
            r = await self.client.get(base + "?publisher=%s" % self.pub.pk)
            self.assertEqual(r.json()["count"], 2)
            r = await self.client.get(base + "?title=OOK-03")
            self.assertEqual(r.json()["count"], 1)
            r = await self.client.get(base + "?author=%s&title=book-0" % self.a2.pk)
            self.assertEqual(r.json()["count"], 3)
            # Unknown related pk: a validation error, not a 500 and not a query
            # on the sync connection.
            r = await self.client.get(base + "?author=999999")
            self.assertEqual(r.status_code, 400, r.content[:300])
            self.assertIn("author", r.json())

    async def test_auto_filterset_from_filterset_fields(self):
        base = "/api/autofiltered-books/"
        with no_sync_queries():
            r = await self.client.get(base + "?author=%s" % self.a2.pk)
            self.assertEqual(r.status_code, 200, r.content[:300])
            self.assertEqual(r.json()["count"], 3)
            r = await self.client.get(base + "?publisher=%s&author=%s" % (self.pub.pk, self.a2.pk))
            self.assertEqual(r.json()["count"], 1)
            r = await self.client.get(base + "?publisher=abc")
            self.assertEqual(r.status_code, 400)
