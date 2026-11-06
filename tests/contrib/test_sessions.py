import datetime

from asgiref.sync import sync_to_async
from django.contrib.sessions.backends.base import CreateError
from django.contrib.sessions.models import Session
from django.db import connections
from django.utils import timezone

from django_async_backend.contrib.sessions import SessionStore
from django_async_backend.test import AsyncioTestCase


async def _close_sync_connections():
    # Tests that compare against the thread-backed sync ORM open a sync
    # connection in the worker thread; close it there, or the test database
    # cannot be dropped at the end of the run.
    await sync_to_async(connections.close_all)()


class AsyncSessionStoreTests(AsyncioTestCase):
    async def test_create_save_load_on_the_async_connection(self):
        store = SessionStore()
        await store.aset("k", "v")
        await store.asave()
        key = store.session_key
        self.assertIsNotNone(key)

        # Written inside the test transaction on the async connection, so
        # the sync ORM cannot see it: proof that no thread hop happened.
        self.assertTrue(
            await Session.async_objects.filter(session_key=key).aexists()
        )
        self.assertFalse(
            await Session.objects.filter(session_key=key).aexists()
        )
        await _close_sync_connections()

        other = SessionStore(key)
        self.assertEqual(await other.aget("k"), "v")
        self.assertTrue(await other.aexists(key))

    async def test_update_existing(self):
        store = SessionStore()
        await store.aset("n", 1)
        await store.asave()
        await store.aset("n", 2)
        await store.asave()
        self.assertEqual(await SessionStore(store.session_key).aget("n"), 2)
        self.assertEqual(await Session.async_objects.acount(), 1)

    async def test_must_create_conflict(self):
        store = SessionStore()
        await store.asave()
        dup = SessionStore(store.session_key)
        await dup.aset("x", 1)
        with self.assertRaises(CreateError):
            await dup.asave(must_create=True)

    async def test_adelete(self):
        store = SessionStore()
        await store.asave()
        key = store.session_key
        await store.adelete()
        self.assertFalse(await store.aexists(key))

    async def test_expired_session_loads_empty(self):
        store = SessionStore()
        await store.aset("k", "v")
        await store.asave()
        await Session.async_objects.filter(
            session_key=store.session_key
        ).aupdate(expire_date=timezone.now() - datetime.timedelta(days=1))
        self.assertEqual(await SessionStore(store.session_key).aload(), {})

    async def test_aclear_expired(self):
        live = SessionStore()
        await live.asave()
        dead = SessionStore()
        await dead.asave()
        await Session.async_objects.filter(
            session_key=dead.session_key
        ).aupdate(expire_date=timezone.now() - datetime.timedelta(days=1))
        await SessionStore.aclear_expired()
        keys = [
            k
            async for k in Session.async_objects.values_list(
                "session_key", flat=True
            )
        ]
        self.assertEqual(keys, [live.session_key])

    async def test_acycle_key_keeps_data(self):
        store = SessionStore()
        await store.aset("k", "v")
        await store.asave()
        old = store.session_key
        await store.acycle_key()
        self.assertNotEqual(store.session_key, old)
        self.assertEqual(await SessionStore(store.session_key).aget("k"), "v")
        self.assertFalse(await store.aexists(old))
