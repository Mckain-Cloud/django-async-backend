"""End-to-end tests of the async admin through Django's ASGI handler.

Every request is made with ``AsyncClient`` while a guard turns any *sync*
database cursor into an error, so a thread hop anywhere in the admin fails
the test.
"""

import contextlib
import re
from unittest import mock

from asgiref.sync import sync_to_async
from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.db import connections
from django.db.backends.base.base import BaseDatabaseWrapper
from django.test import AsyncClient
from test_app.models import (
    M2MOwnerModel,
    M2MTagModel,
    SelectRelatedAuthorModel,
    SelectRelatedBookModel,
    SelectRelatedPublisherModel,
    TestModel,
)

from django_async_backend.test import AsyncioTransactionTestCase

User = get_user_model()


class SyncQueryAttempted(AssertionError):
    pass


@contextlib.contextmanager
def no_sync_queries():
    def cursor(self):
        raise SyncQueryAttempted(
            "a synchronous database cursor was opened (alias %r)" % self.alias
        )

    with mock.patch.object(BaseDatabaseWrapper, "cursor", cursor):
        yield


async def _close_sync_connections():
    await sync_to_async(connections.close_all)()


class AsyncAdminTests(AsyncioTransactionTestCase):
    async def asyncSetUp(self):
        self.client = AsyncClient()
        self.admin = User(username="root", is_staff=True, is_superuser=True)
        self.admin.set_password("pw")
        await self.admin.async_save()
        self.pub = await SelectRelatedPublisherModel.async_objects.acreate(
            name="pub"
        )
        self.a1 = await SelectRelatedAuthorModel.async_objects.acreate(
            name="alice"
        )
        self.a2 = await SelectRelatedAuthorModel.async_objects.acreate(
            name="bob"
        )
        self.books = [
            await SelectRelatedBookModel.async_objects.acreate(
                title="book%02d" % i,
                author=self.a1 if i % 2 else self.a2,
                publisher=self.pub if i < 4 else None,
            )
            for i in range(8)
        ]

    async def asyncTearDown(self):
        await _close_sync_connections()
        for model in (
            LogEntry,
            SelectRelatedBookModel,
            SelectRelatedAuthorModel,
            SelectRelatedPublisherModel,
            M2MOwnerModel,
            M2MTagModel,
            TestModel,
            User,
        ):
            await model.async_objects.all().adelete()
        from django.contrib.sessions.models import Session

        await Session.async_objects.all().adelete()

    async def _login(self):
        with no_sync_queries():
            r = await self.client.get("/admin/login/")
            self.assertEqual(r.status_code, 200)
            r = await self.client.post(
                "/admin/login/?next=/admin/",
                {"username": "root", "password": "pw", "next": "/admin/"},
            )
        self.assertEqual(r.status_code, 302, r.content[:300])
        self.assertEqual(r["Location"], "/admin/")

    def _csrf(self, response):
        return re.search(
            r'name="csrfmiddlewaretoken" value="([^"]+)"',
            response.content.decode(),
        ).group(1)

    async def test_login_index_and_app_index(self):
        await self._login()
        with no_sync_queries():
            r = await self.client.get("/admin/")
            self.assertEqual(r.status_code, 200)
            self.assertIn(b"Select related book models", r.content)
            r = await self.client.get("/admin/test_app/")
            self.assertEqual(r.status_code, 200)
            r = await self.client.get("/admin/nope/")
            self.assertEqual(r.status_code, 404)

    async def test_anonymous_is_redirected(self):
        with no_sync_queries():
            r = await self.client.get(
                "/admin/test_app/selectrelatedbookmodel/"
            )
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin/login/", r["Location"])

    async def test_changelist_pagination_filters_search(self):
        await self._login()
        url = "/admin/test_app/selectrelatedbookmodel/"
        with no_sync_queries():
            r = await self.client.get(url)
            self.assertEqual(r.status_code, 200)
            body = r.content.decode()
            self.assertEqual(
                body.count('class="field-title"'), 5
            )  # list_per_page
            self.assertIn("8 select related book models", body)
            self.assertIn("By author", body)
            self.assertIn("By publisher", body)
            self.assertIn("By title", body)

            r = await self.client.get(url, {"p": 2})
            self.assertEqual(
                r.content.decode().count('class="field-title"'), 3
            )

            r = await self.client.get(url, {"author__id__exact": self.a1.pk})
            self.assertEqual(
                r.content.decode().count('class="field-title"'), 4
            )

            r = await self.client.get(url, {"q": "book07"})
            self.assertEqual(
                r.content.decode().count('class="field-title"'), 1
            )

    async def test_add_change_delete_history(self):
        await self._login()
        base = "/admin/test_app/selectrelatedbookmodel/"
        with no_sync_queries():
            r = await self.client.get(base + "add/")
            self.assertEqual(r.status_code, 200)
            self.assertIn(
                '<option value="%s">' % self.a1.pk, r.content.decode()
            )

            r = await self.client.post(
                base + "add/",
                {"title": "added", "author": self.a2.pk, "_save": "Save"},
            )
            self.assertEqual(r.status_code, 302, r.content[:500])
        book = await SelectRelatedBookModel.async_objects.aget(title="added")
        self.assertEqual(book.author_id, self.a2.pk)
        self.assertEqual(
            await LogEntry.async_objects.filter(
                object_id=str(book.pk)
            ).acount(),
            1,
        )

        with no_sync_queries():
            r = await self.client.get(base + "%s/change/" % book.pk)
            self.assertEqual(r.status_code, 200)
            self.assertIn("added", r.content.decode())
            r = await self.client.post(
                base + "%s/change/" % book.pk,
                {"title": "renamed", "author": self.a1.pk, "_continue": "1"},
            )
            self.assertEqual(r.status_code, 302, r.content[:500])
        await book.async_refresh_from_db()
        self.assertEqual((book.title, book.author_id), ("renamed", self.a1.pk))

        with no_sync_queries():
            r = await self.client.get(base + "%s/history/" % book.pk)
            self.assertEqual(r.status_code, 200)
            self.assertIn("Changed", r.content.decode())

            r = await self.client.get(base + "%s/delete/" % book.pk)
            self.assertEqual(r.status_code, 200)
            self.assertIn("Are you sure", r.content.decode())
            r = await self.client.post(
                base + "%s/delete/" % book.pk, {"post": "yes"}
            )
            self.assertEqual(r.status_code, 302)
        self.assertFalse(
            await SelectRelatedBookModel.async_objects.filter(
                pk=book.pk
            ).aexists()
        )

    async def test_delete_with_cascades_lists_children(self):
        await self._login()
        url = (
            "/admin/test_app/selectrelatedauthormodel/%s/delete/" % self.a1.pk
        )
        mine = [b for b in self.books if b.author_id == self.a1.pk]
        with no_sync_queries():
            r = await self.client.get(url)
            self.assertEqual(r.status_code, 200)
            body = r.content.decode()
            for b in mine:
                self.assertIn(
                    "/admin/test_app/selectrelatedbookmodel/%s/change/" % b.pk,
                    body,
                )
            r = await self.client.post(url, {"post": "yes"})
            self.assertEqual(r.status_code, 302)
        self.assertFalse(
            await SelectRelatedAuthorModel.async_objects.filter(
                pk=self.a1.pk
            ).aexists()
        )
        self.assertEqual(
            await SelectRelatedBookModel.async_objects.filter(
                pk__in=[b.pk for b in mine]
            ).acount(),
            0,
        )

    async def test_validation_errors_are_rendered(self):
        await self._login()
        with no_sync_queries():
            r = await self.client.post(
                "/admin/test_app/selectrelatedbookmodel/add/",
                {"title": "book01", "author": self.a1.pk, "_save": "Save"},
            )
        self.assertEqual(r.status_code, 200)
        self.assertIn("already exists", r.content.decode())

    async def test_inlines_and_m2m(self):
        await self._login()
        url = (
            "/admin/test_app/selectrelatedauthormodel/%s/change/" % self.a1.pk
        )
        with no_sync_queries():
            r = await self.client.get(url)
            self.assertEqual(r.status_code, 200)
            body = r.content.decode()
            self.assertIn("books-TOTAL_FORMS", body)
            self.assertIn("book01", body)
            data = {
                "name": "alice",
                "books-TOTAL_FORMS": "5",
                "books-INITIAL_FORMS": "4",
                "books-MIN_NUM_FORMS": "0",
                "books-MAX_NUM_FORMS": "1000",
                "_save": "Save",
            }
            mine = [b for b in self.books if b.author_id == self.a1.pk]
            for i, b in enumerate(mine):
                data["books-%d-id" % i] = b.pk
                data["books-%d-title" % i] = b.title
                data["books-%d-publisher" % i] = b.publisher_id or ""
            data["books-0-DELETE"] = "on"
            data["books-4-id"] = ""
            data["books-4-title"] = "inline-added"
            data["books-4-publisher"] = self.pub.pk
            r = await self.client.post(url, data)
            self.assertEqual(r.status_code, 302, r.content[:800])
        titles = sorted(
            [
                t
                async for t in self.a1.books.async_objects.values_list(
                    "title", flat=True
                )
            ]
        )
        self.assertNotIn(mine[0].title, titles)
        self.assertIn("inline-added", titles)

        t1 = await M2MTagModel.async_objects.acreate(name="t1")
        t2 = await M2MTagModel.async_objects.acreate(name="t2")
        with no_sync_queries():
            r = await self.client.post(
                "/admin/test_app/m2mownermodel/add/",
                {"name": "owner", "tags": [t1.pk, t2.pk], "_save": "Save"},
            )
            self.assertEqual(r.status_code, 302, r.content[:500])
            owner = await M2MOwnerModel.async_objects.aget(name="owner")
            r = await self.client.get(
                "/admin/test_app/m2mownermodel/%s/change/" % owner.pk
            )
            self.assertEqual(r.status_code, 200)
            self.assertIn(
                '<option value="%s" selected>' % t1.pk, r.content.decode()
            )
        self.assertEqual(await owner.tags.async_objects.acount(), 2)

    async def test_delete_selected_action(self):
        await self._login()
        url = "/admin/test_app/selectrelatedbookmodel/"
        pks = [str(b.pk) for b in self.books[:3]]
        with no_sync_queries():
            r = await self.client.post(
                url,
                {
                    "action": "delete_selected",
                    "_selected_action": pks,
                    "index": "0",
                },
            )
            self.assertEqual(r.status_code, 200)
            self.assertIn("Are you sure", r.content.decode())
            r = await self.client.post(
                url,
                {
                    "action": "delete_selected",
                    "_selected_action": pks,
                    "post": "yes",
                },
            )
            self.assertEqual(r.status_code, 302)
        self.assertEqual(
            await SelectRelatedBookModel.async_objects.acount(), 5
        )

    async def test_list_editable_and_raw_id(self):
        await self._login()
        parent = await TestModel.async_objects.acreate(name="parent", value=1)
        child = await TestModel.async_objects.acreate(
            name="child", value=2, relative=parent
        )
        url = "/admin/test_app/testmodel/"
        with no_sync_queries():
            r = await self.client.get(url)
            self.assertEqual(r.status_code, 200)
            self.assertIn("form-TOTAL_FORMS", r.content.decode())
            r = await self.client.post(
                url,
                {
                    "form-TOTAL_FORMS": "2",
                    "form-INITIAL_FORMS": "2",
                    "form-MIN_NUM_FORMS": "0",
                    "form-MAX_NUM_FORMS": "1000",
                    "form-0-id": child.pk,
                    "form-0-value": "20",
                    "form-1-id": parent.pk,
                    "form-1-value": "1",
                    "_save": "Save",
                },
            )
            self.assertEqual(r.status_code, 302, r.content[:500])
            r = await self.client.get(url + "%s/change/" % child.pk)
            self.assertEqual(r.status_code, 200)
            self.assertIn(
                "TestModel object (%s)" % parent.pk, r.content.decode()
            )  # raw-id label
        await child.async_refresh_from_db()
        self.assertEqual(child.value, 20)

    async def test_autocomplete_logout_password_change(self):
        await self._login()
        with no_sync_queries():
            r = await self.client.get(
                "/admin/autocomplete/",
                {
                    "app_label": "test_app",
                    "model_name": "selectrelatedbookmodel",
                    "field_name": "author",
                    "term": "ali",
                },
            )
            self.assertEqual(r.status_code, 200)
            self.assertEqual(
                [x["text"] for x in r.json()["results"]], [str(self.a1)]
            )

            r = await self.client.get("/admin/password_change/")
            self.assertEqual(r.status_code, 200)
            r = await self.client.post(
                "/admin/password_change/",
                {
                    "old_password": "pw",
                    "new_password1": "a-much-longer-password-1",
                    "new_password2": "a-much-longer-password-1",
                },
            )
            self.assertEqual(r.status_code, 302, r.content[:500])
            r = await self.client.get("/admin/")  # still logged in
            self.assertEqual(r.status_code, 200)

            r = await self.client.post("/admin/logout/")
            self.assertEqual(r.status_code, 200)
            r = await self.client.get("/admin/")
            self.assertEqual(r.status_code, 302)
        await self.admin.async_refresh_from_db()
        self.assertTrue(self.admin.check_password("a-much-longer-password-1"))
