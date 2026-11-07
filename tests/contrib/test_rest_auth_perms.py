"""Async REST framework authenticators and object permissions, with the
backend from the admin object-permission tests (sync has_perm() raises)."""

import base64

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.sessions.models import Session
from django.test import AsyncClient
from rest_framework.authtoken.models import Token
from test_app.models import (
    SelectRelatedAuthorModel,
    SelectRelatedBookModel,
)

from django_async_backend.test import (
    AsyncioTransactionTestCase,
    override_settings,
)
from tests.contrib.test_admin import (
    _close_sync_connections,
    no_sync_queries,
)

User = get_user_model()


def _basic(username, password):
    raw = ("%s:%s" % (username, password)).encode()
    return {"headers": {"Authorization": "Basic " + base64.b64encode(raw).decode()}}


def _token(key):
    return {"headers": {"Authorization": "Token " + key}}


@override_settings(
    AUTHENTICATION_BACKENDS=[
        "django_async_backend.contrib.auth.AsyncModelBackend",
        "tests.contrib.test_admin_object_perms.OwnBooksBackend",
    ]
)
class AuthAndObjectPermissionTests(AsyncioTransactionTestCase):
    async def asyncSetUp(self):
        self.client = AsyncClient()
        self.user = User(username="staff", is_staff=True)
        self.user.set_password("pw")
        await self.user.async_save()
        # DjangoObjectPermissions requires the model-level permission first;
        # the object-level backend then decides per row.
        for codename in (
            "view_selectrelatedbookmodel",
            "change_selectrelatedbookmodel",
            "delete_selectrelatedbookmodel",
        ):
            perm = await Permission.async_objects.aget(codename=codename)
            await self.user.user_permissions.async_objects.aadd(perm)
        self.author = await SelectRelatedAuthorModel.async_objects.acreate(name="a")
        self.mine = await SelectRelatedBookModel.async_objects.acreate(
            title="mine-1", author=self.author
        )
        self.theirs = await SelectRelatedBookModel.async_objects.acreate(
            title="theirs-1", author=self.author
        )

    async def asyncTearDown(self):
        await _close_sync_connections()
        for model in (Token, SelectRelatedBookModel, SelectRelatedAuthorModel, User, Session):
            await model.async_objects.all().adelete()

    async def test_basic_auth_and_object_permissions(self):
        base = "/api/objperm-books/"
        good = _basic("staff", "pw")
        self.assertTrue(hasattr(self.user, "ahas_perms"))
        self.assertTrue(
            await self.user.ahas_perms(["test_app.view_selectrelatedbookmodel"])
        )
        self.assertTrue(
            await self.user.ahas_perms(
                ["test_app.change_selectrelatedbookmodel"], self.mine
            )
        )
        with no_sync_queries():
            r = await self.client.get(base)
            self.assertEqual(r.status_code, 401)
            r = await self.client.get(base, **_basic("staff", "wrong"))
            self.assertEqual(r.status_code, 401)
            r = await self.client.get(base, **good)
            self.assertEqual(r.status_code, 200, r.content[:300])
            self.assertEqual(r.json()["count"], 2)
            r = await self.client.get(base + "%s/" % self.theirs.pk, **good)
            self.assertEqual(r.status_code, 200)
            # Object-level change permission comes from the backend's query.
            r = await self.client.patch(
                base + "%s/" % self.mine.pk,
                data='{"title": "mine-2"}',
                content_type="application/json",
                **good,
            )
            self.assertEqual(r.status_code, 200, r.content[:300])
            r = await self.client.patch(
                base + "%s/" % self.theirs.pk,
                data='{"title": "x"}',
                content_type="application/json",
                **good,
            )
            self.assertEqual(r.status_code, 403)
            r = await self.client.delete(base + "%s/" % self.mine.pk, **good)
            self.assertEqual(r.status_code, 204)
            r = await self.client.delete(base + "%s/" % self.theirs.pk, **good)
            self.assertEqual(r.status_code, 403)

    async def test_token_auth(self):
        token = await Token.async_objects.acreate(user=self.user)
        self.assertTrue(token.key)  # async_save twin generated it
        base = "/api/token-books/"
        with no_sync_queries():
            r = await self.client.get(base)
            self.assertEqual(r.status_code, 401)
            r = await self.client.get(base, **_token("nope"))
            self.assertEqual(r.status_code, 401)
            self.assertIn("Invalid token", r.json()["detail"])
            r = await self.client.get(base, **_token(token.key))
            self.assertEqual(r.status_code, 200, r.content[:300])
            self.assertEqual(len(r.json()), 2)
        self.user.is_active = False
        await self.user.async_save()
        with no_sync_queries():
            r = await self.client.get(base, **_token(token.key))
            self.assertEqual(r.status_code, 401)
