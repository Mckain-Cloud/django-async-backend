import asyncio

from asgiref.sync import sync_to_async
from django.contrib.auth import (
    alogin,
    get_user_model,
)
from django.contrib.auth.models import (
    Group,
    Permission,
)
from django.contrib.auth.signals import user_logged_in
from django.db import connections
from django.test import AsyncRequestFactory

from django_async_backend.contrib.auth import (
    AsyncModelBackend,
    aupdate_last_login,
)
from django_async_backend.contrib.sessions import SessionStore
from django_async_backend.db.transaction import async_atomic
from django_async_backend.test import AsyncioTestCase

User = get_user_model()
BACKEND = "django_async_backend.contrib.auth.AsyncModelBackend"


async def make_user(username, password="pw", **fields):
    # Created on the async connection: inside the test transaction it is
    # invisible to the sync ORM, so anything that finds it proves the async
    # path was used.
    user = User(username=username, **fields)
    user.set_password(password)
    await user.async_save()
    return user


async def _close_sync_connections():
    # Tests that compare against the thread-backed sync ORM open a sync
    # connection in the worker thread; close it there, or the test database
    # cannot be dropped at the end of the run.
    await sync_to_async(connections.close_all)()


class AsyncModelBackendTests(AsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = AsyncModelBackend()
        self.user = await make_user("alice")

    async def test_aauthenticate(self):
        self.assertEqual(
            await User.objects.filter(username="alice").acount(), 0
        )
        await _close_sync_connections()
        user = await self.backend.aauthenticate(None, "alice", "pw")
        self.assertEqual(user.pk, self.user.pk)
        self.assertIsNone(await self.backend.aauthenticate(None, "alice", "x"))
        self.assertIsNone(await self.backend.aauthenticate(None, "bob", "pw"))
        self.assertIsNone(await self.backend.aauthenticate(None))

    async def test_aauthenticate_by_username_field_kwarg(self):
        user = await self.backend.aauthenticate(
            None, password="pw", **{User.USERNAME_FIELD: "alice"}
        )
        self.assertEqual(user.pk, self.user.pk)

    async def test_inactive_user_is_rejected(self):
        self.user.is_active = False
        await self.user.async_save(update_fields=["is_active"])
        self.assertIsNone(
            await self.backend.aauthenticate(None, "alice", "pw")
        )
        self.assertIsNone(await self.backend.aget_user(self.user.pk))

    async def test_aget_user(self):
        self.assertEqual(
            (await self.backend.aget_user(self.user.pk)).pk, self.user.pk
        )
        self.assertIsNone(await self.backend.aget_user(0))

    async def test_user_and_group_permissions(self):
        perm = await Permission.async_objects.aget(codename="add_testmodel")
        group_perm = await Permission.async_objects.aget(
            codename="change_testmodel"
        )
        await self.user.user_permissions.async_objects.aadd(perm)
        group = Group(name="editors")
        await group.async_save()
        await group.permissions.async_objects.aadd(group_perm)
        await self.user.groups.async_objects.aadd(group)

        self.assertEqual(
            await self.backend.aget_user_permissions(self.user),
            {"test_app.add_testmodel"},
        )
        self.assertEqual(
            await self.backend.aget_group_permissions(self.user),
            {"test_app.change_testmodel"},
        )
        self.assertEqual(
            await self.backend.aget_all_permissions(self.user),
            {"test_app.add_testmodel", "test_app.change_testmodel"},
        )
        self.assertTrue(
            await self.backend.ahas_perm(self.user, "test_app.add_testmodel")
        )
        self.assertFalse(
            await self.backend.ahas_perm(
                self.user, "test_app.delete_testmodel"
            )
        )
        self.assertTrue(
            await self.backend.ahas_module_perms(self.user, "test_app")
        )
        self.assertFalse(
            await self.backend.ahas_module_perms(self.user, "auth")
        )

    async def test_superuser_has_everything(self):
        root = await make_user("root", is_superuser=True)
        perms = await self.backend.aget_all_permissions(root)
        self.assertIn("test_app.add_testmodel", perms)
        self.assertIn("auth.add_user", perms)

    async def test_permissions_through_the_user_model(self):
        perm = await Permission.async_objects.aget(codename="add_testmodel")
        await self.user.user_permissions.async_objects.aadd(perm)
        with self.settings(AUTHENTICATION_BACKENDS=[BACKEND]):
            self.assertTrue(
                await self.user.ahas_perm("test_app.add_testmodel")
            )
            self.assertEqual(
                await self.user.aget_all_permissions(),
                {"test_app.add_testmodel"},
            )

    async def test_awith_perm(self):
        perm = await Permission.async_objects.aget(codename="add_testmodel")
        await self.user.user_permissions.async_objects.aadd(perm)
        await make_user("nobody")
        users = await self.backend.awith_perm("test_app.add_testmodel")
        self.assertEqual(
            [u.username async for u in users.order_by("username")], ["alice"]
        )


async def _close_sync_connections():
    # Tests that compare against the thread-backed sync ORM open a sync
    # connection in the worker thread; close it there, or the test database
    # cannot be dropped at the end of the run.
    await sync_to_async(connections.close_all)()


class AsyncLastLoginTests(AsyncioTestCase):
    async def asyncSetUp(self):
        self.user = await make_user("alice")

    async def test_async_receiver_is_connected(self):
        receivers = [r[1]() for r in user_logged_in.receivers]
        self.assertIn(aupdate_last_login, receivers)

    async def test_alogin_updates_last_login_on_the_async_connection(self):
        request = AsyncRequestFactory().get("/")
        request.session = SessionStore()
        self.assertIsNone(self.user.last_login)

        await alogin(request, self.user, backend=BACKEND)

        self.assertIsNotNone(self.user.last_login)
        stored = await User.async_objects.aget(pk=self.user.pk)
        self.assertEqual(stored.last_login, self.user.last_login)
        self.assertEqual(
            await request.session.aget("_auth_user_id"), str(self.user.pk)
        )

    async def test_signup_then_login_in_one_transaction(self):
        # Regression: the receiver used to run on a second connection, where
        # the uncommitted user row was invisible (NotUpdated).
        async with async_atomic():
            user = await make_user("newbie")
            request = AsyncRequestFactory().get("/")
            request.session = SessionStore()
            await alogin(request, user, backend=BACKEND)
            self.assertIsNotNone(user.last_login)

    async def test_login_after_updating_the_user_in_the_same_transaction(self):
        # Regression: a second connection would block on the row lock held
        # by this transaction, deadlocking the request against itself.
        async with async_atomic():
            self.user.first_name = "touched"
            await self.user.async_save(update_fields=["first_name"])
            request = AsyncRequestFactory().get("/")
            request.session = SessionStore()
            await asyncio.wait_for(
                alogin(request, self.user, backend=BACKEND), timeout=5
            )
        stored = await User.async_objects.aget(pk=self.user.pk)
        self.assertEqual(stored.first_name, "touched")
        self.assertIsNotNone(stored.last_login)
