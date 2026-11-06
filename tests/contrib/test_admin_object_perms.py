"""An auth backend that decides per object with a query, and only has an
async implementation: the async admin must never reach its sync path."""

from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import BaseBackend
from django.contrib.auth.models import Permission
from django.contrib.sessions.models import Session
from django.test import AsyncClient
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
CALLS = []


class OwnBooksBackend(BaseBackend):
    """Grants change/delete on books whose title starts with "mine-"."""

    def has_perm(self, user_obj, perm, obj=None):
        raise AssertionError("sync has_perm() must not be called")

    async def ahas_perm(self, user_obj, perm, obj=None):
        CALLS.append((perm, getattr(obj, "pk", None)))
        if obj is None or not isinstance(obj, SelectRelatedBookModel):
            return False
        if perm not in (
            "test_app.change_selectrelatedbookmodel",
            "test_app.delete_selectrelatedbookmodel",
        ):
            return False
        return await SelectRelatedBookModel.async_objects.filter(
            pk=obj.pk, title__startswith="mine-"
        ).aexists()

    async def ahas_module_perms(self, user_obj, app_label):
        return False


@override_settings(
    AUTHENTICATION_BACKENDS=[
        "django_async_backend.contrib.auth.AsyncModelBackend",
        "tests.contrib.test_admin_object_perms.OwnBooksBackend",
    ]
)
class ObjectPermissionAdminTests(AsyncioTransactionTestCase):
    async def asyncSetUp(self):
        CALLS.clear()
        self.client = AsyncClient()
        self.user = User(username="staff", is_staff=True)
        self.user.set_password("pw")
        await self.user.async_save()
        # Model-level: may view books and authors; may not change/delete.
        for codename in (
            "view_selectrelatedbookmodel",
            "view_selectrelatedauthormodel",
        ):
            perm = await Permission.async_objects.aget(codename=codename)
            await self.user.user_permissions.async_objects.aadd(perm)
        self.author = await SelectRelatedAuthorModel.async_objects.acreate(
            name="a"
        )
        self.mine = await SelectRelatedBookModel.async_objects.acreate(
            title="mine-1", author=self.author
        )
        self.theirs = await SelectRelatedBookModel.async_objects.acreate(
            title="theirs-1", author=self.author
        )
        r = await self.client.post(
            "/admin/login/", {"username": "staff", "password": "pw"}
        )
        assert r.status_code == 302, r.content[:200]

    async def asyncTearDown(self):
        await _close_sync_connections()
        for model in (
            LogEntry,
            SelectRelatedBookModel,
            SelectRelatedAuthorModel,
            User,
            Session,
        ):
            await model.async_objects.all().adelete()

    async def test_change_view_honours_object_permission(self):
        base = "/admin/test_app/selectrelatedbookmodel/"
        self.assertTrue(
            await self.user.ahas_perm(
                "test_app.change_selectrelatedbookmodel", self.mine
            ),
            CALLS,
        )
        self.assertFalse(
            await self.user.ahas_perm(
                "test_app.change_selectrelatedbookmodel", self.theirs
            )
        )
        # Step through the admin's own wiring on a bare request.
        from admin_urls import site
        from django.test import AsyncRequestFactory

        model_admin = site._registry[SelectRelatedBookModel]
        request = AsyncRequestFactory().get("/")
        request.user = self.user
        await model_admin.aprepare_permissions(request, self.mine)
        self.assertTrue(
            await model_admin.ahas_change_permission(request, self.mine)
        )
        self.assertTrue(model_admin.has_change_permission(request, self.mine))
        self.assertTrue(
            model_admin.has_view_or_change_permission(request, self.mine)
        )
        with no_sync_queries():
            r = await self.client.get(base + "%s/change/" % self.mine.pk)
            self.assertEqual(r.status_code, 200, (r.content[:600], CALLS))
            body = r.content.decode()
            self.assertIn('name="title"', body)  # editable
            self.assertIn("Change select related book model", body)

            r = await self.client.get(base + "%s/change/" % self.theirs.pk)
            self.assertEqual(r.status_code, 200)
            body = r.content.decode()
            self.assertNotIn('name="title"', body)  # read-only
            self.assertIn("View select related book model", body)

            r = await self.client.post(
                base + "%s/change/" % self.mine.pk,
                {"title": "mine-2", "author": self.author.pk, "_save": "1"},
            )
            self.assertEqual(r.status_code, 302, r.content[:300])
            r = await self.client.post(
                base + "%s/change/" % self.theirs.pk,
                {"title": "x", "author": self.author.pk, "_save": "1"},
            )
            self.assertEqual(r.status_code, 403)
        await self.mine.async_refresh_from_db()
        self.assertEqual(self.mine.title, "mine-2")
        self.assertTrue(any(pk == self.mine.pk for _, pk in CALLS), CALLS)

    async def test_delete_view_and_changelist(self):
        base = "/admin/test_app/selectrelatedbookmodel/"
        with no_sync_queries():
            r = await self.client.get(base)
            self.assertEqual(r.status_code, 200)
            self.assertIn(
                "Select select related book model to view", r.content.decode()
            )
            r = await self.client.get(base + "%s/delete/" % self.theirs.pk)
            self.assertEqual(r.status_code, 403)
            r = await self.client.get(base + "%s/delete/" % self.mine.pk)
            self.assertEqual(r.status_code, 200)
            r = await self.client.post(
                base + "%s/delete/" % self.mine.pk, {"post": "yes"}
            )
            self.assertEqual(r.status_code, 302)
        self.assertFalse(
            await SelectRelatedBookModel.async_objects.filter(
                pk=self.mine.pk
            ).aexists()
        )
