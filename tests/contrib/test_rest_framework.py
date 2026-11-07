"""Django REST framework viewsets served on the async connection.

Every request below runs under ``no_sync_queries()``: a sync cursor anywhere
(authentication, pagination count, related lookups, unique validators,
writes) fails the test."""

import json

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.sessions.models import Session
from django.test import AsyncClient
from test_app.models import (
    M2MOwnerModel,
    M2MTagModel,
    SelectRelatedAuthorModel,
    SelectRelatedBookModel,
    SelectRelatedPublisherModel,
)

from django_async_backend.test import AsyncioTransactionTestCase
from tests.contrib.test_admin import (
    _close_sync_connections,
    no_sync_queries,
)

User = get_user_model()


class _Base(AsyncioTransactionTestCase):
    async def asyncSetUp(self):
        self.client = AsyncClient()
        self.author = await SelectRelatedAuthorModel.async_objects.acreate(name="ann")
        self.author2 = await SelectRelatedAuthorModel.async_objects.acreate(name="bob")
        self.publisher = await SelectRelatedPublisherModel.async_objects.acreate(
            name="pub"
        )
        self.books = [
            await SelectRelatedBookModel.async_objects.acreate(
                title="book-%02d" % i,
                author=self.author if i % 2 else self.author2,
                publisher=self.publisher if i % 3 else None,
            )
            for i in range(7)
        ]
        self.tags = [
            await M2MTagModel.async_objects.acreate(name="t%d" % i) for i in range(3)
        ]
        self.owner = await M2MOwnerModel.async_objects.acreate(name="owner-1")
        await self.owner.tags.async_objects.aset(self.tags[:2])

    async def asyncTearDown(self):
        await _close_sync_connections()
        for model in (
            M2MOwnerModel,
            M2MTagModel,
            SelectRelatedBookModel,
            SelectRelatedPublisherModel,
            SelectRelatedAuthorModel,
            User,
            Session,
        ):
            await model.async_objects.all().adelete()

    async def _login(self, username="staff", perms=(), **flags):
        user = User(username=username, is_staff=True, **flags)
        user.set_password("pw")
        await user.async_save()
        for codename in perms:
            perm = await Permission.async_objects.aget(codename=codename)
            await user.user_permissions.async_objects.aadd(perm)
        r = await self.client.post(
            "/admin/login/", {"username": username, "password": "pw"}
        )
        assert r.status_code == 302, r.content[:200]
        # Session authentication enforces CSRF on authenticated requests.
        self.csrf = self.client.cookies["csrftoken"].value
        return user

    def _json(self, method, path, payload=None):
        kwargs = {}
        if payload is not None:
            kwargs.update(
                data=json.dumps(payload),
                content_type="application/json",
            )
        if hasattr(self, "csrf"):
            kwargs["headers"] = {"X-CSRFToken": self.csrf}
        return getattr(self.client, method)(path, **kwargs)


class ReadTests(_Base):
    async def test_list_is_paginated_and_loads_relations(self):
        with no_sync_queries():
            r = await self.client.get("/api/books/")
            self.assertEqual(r.status_code, 200, r.content[:300])
            body = r.json()
            self.assertEqual(body["count"], 7)
            self.assertEqual(len(body["results"]), 5)
            self.assertTrue(body["next"].endswith("/api/books/?page=2"))
            first = body["results"][0]
            self.assertEqual(first["title"], "book-00")
            self.assertEqual(first["author"], self.author2.pk)
            self.assertEqual(first["author_name"], "bob")
            self.assertEqual(first["shout"], "BOOK-00 BY BOB")
            self.assertIsNone(first["publisher"])

            r = await self.client.get("/api/books/?page=last")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(len(r.json()["results"]), 2)

            r = await self.client.get("/api/books/?page=9")
            self.assertEqual(r.status_code, 404)

    async def test_retrieve_and_404(self):
        with no_sync_queries():
            r = await self.client.get("/api/books/%s/" % self.books[1].pk)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["author_name"], "ann")
            r = await self.client.get("/api/books/999999/")
            self.assertEqual(r.status_code, 404)
            r = await self.client.get("/api/books/not-a-pk/")
            self.assertEqual(r.status_code, 404)

    async def test_many_to_many_representation_and_limit_offset(self):
        with no_sync_queries():
            r = await self.client.get("/api/owners/?limit=5")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertEqual(body["count"], 1)
            row = body["results"][0]
            self.assertEqual(sorted(row["tags"]), sorted(t.pk for t in self.tags[:2]))
            expected = sorted(str(t) for t in self.tags[:2])
            self.assertEqual(sorted(row["tag_names"]), expected)
            r = await self.client.get("/api/owners/%s/" % self.owner.pk)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(sorted(r.json()["tag_names"]), expected)


class WriteTests(_Base):
    async def test_anonymous_cannot_write(self):
        with no_sync_queries():
            r = await self._json("post", "/api/books/", {"title": "x", "author": 1})
            self.assertEqual(r.status_code, 403)

    async def test_create_update_delete_with_relations_and_unique(self):
        await self._login()
        with no_sync_queries():
            r = await self._json(
                "post",
                "/api/books/",
                {"title": "new", "author": self.author.pk, "publisher": None},
            )
            self.assertEqual(r.status_code, 201, r.content[:300])
            body = r.json()
            self.assertEqual(body["author_name"], "ann")
            self.assertEqual(body["shout"], "NEW BY ANN")
            pk = body["id"]

            # UniqueValidator on title, async EXISTS.
            r = await self._json(
                "post", "/api/books/", {"title": "new", "author": self.author.pk}
            )
            self.assertEqual(r.status_code, 400)
            self.assertIn("already exists", str(r.json()["title"]).lower())

            # Unknown FK pk -> field error, not 500.
            r = await self._json("post", "/api/books/", {"title": "x", "author": 999999})
            self.assertEqual(r.status_code, 400)
            self.assertIn("does not exist", str(r.json()["author"]))

            # PUT / PATCH
            r = await self._json(
                "put",
                "/api/books/%s/" % pk,
                {"title": "new2", "author": self.author2.pk, "publisher": self.publisher.pk},
            )
            self.assertEqual(r.status_code, 200, r.content[:300])
            self.assertEqual(r.json()["author_name"], "bob")
            self.assertEqual(r.json()["publisher"], self.publisher.pk)
            r = await self._json("patch", "/api/books/%s/" % pk, {"title": "new3"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["title"], "new3")

            # Updating to a title that another row has is still a violation;
            # keeping its own title is not (exclude_current_instance).
            r = await self._json("patch", "/api/books/%s/" % pk, {"title": "book-01"})
            self.assertEqual(r.status_code, 400)
            r = await self._json("patch", "/api/books/%s/" % pk, {"title": "new3"})
            self.assertEqual(r.status_code, 200)

            r = await self._json("delete", "/api/books/%s/" % pk)
            self.assertEqual(r.status_code, 204)
        self.assertFalse(
            await SelectRelatedBookModel.async_objects.filter(pk=pk).aexists()
        )

    async def test_many_to_many_write(self):
        await self._login(
            perms=["add_m2mownermodel", "change_m2mownermodel", "view_m2mownermodel"]
        )
        with no_sync_queries():
            r = await self._json(
                "post",
                "/api/owners/",
                {"name": "owner-2", "tags": [t.pk for t in self.tags]},
            )
            self.assertEqual(r.status_code, 201, r.content[:300])
            pk = r.json()["id"]
            self.assertEqual(
                sorted(r.json()["tag_names"]), sorted(str(t) for t in self.tags)
            )
            r = await self._json(
                "patch", "/api/owners/%s/" % pk, {"tags": [self.tags[2].pk]}
            )
            self.assertEqual(r.status_code, 200, r.content[:300])
            self.assertEqual(r.json()["tag_names"], [str(self.tags[2])])
            # Bad tag pk inside the list.
            r = await self._json("patch", "/api/owners/%s/" % pk, {"tags": [999999]})
            self.assertEqual(r.status_code, 400)
            # No delete permission -> DjangoModelPermissions denies from the
            # warmed permission cache, no query.
            r = await self._json("delete", "/api/owners/%s/" % pk)
            self.assertEqual(r.status_code, 403)
        owner = await M2MOwnerModel.async_objects.aget(pk=pk)
        self.assertEqual(
            [t.pk async for t in owner.tags.async_objects.all()], [self.tags[2].pk]
        )

    async def test_model_permissions_denied_without_perm(self):
        await self._login(perms=["view_m2mownermodel"])
        with no_sync_queries():
            r = await self._json("post", "/api/owners/", {"name": "nope", "tags": []})
            self.assertEqual(r.status_code, 403)
            r = await self.client.get("/api/owners/")
            self.assertEqual(r.status_code, 200)
