"""AsyncDatabaseCache: every operation on the async connection."""

import asyncio

from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.cache import caches
from django.test import AsyncClient

from django_async_backend.core.cache import AsyncDatabaseCache
from django_async_backend.db.transaction import async_atomic
from django_async_backend.test import (
    AsyncioTestCase,
    AsyncioTransactionTestCase,
    override_settings,
)
from tests.contrib.test_admin import _close_sync_connections, no_sync_queries

User = get_user_model()


class AsyncDatabaseCacheTests(AsyncioTestCase):
    def setUp(self):
        self.cache = caches["default"]
        self.assertIsInstance(self.cache, AsyncDatabaseCache)

    async def test_round_trip_and_defaults(self):
        with no_sync_queries():
            self.assertIsNone(await self.cache.aget("missing"))
            self.assertEqual(await self.cache.aget("missing", "dflt"), "dflt")
            await self.cache.aset("k", {"a": [1, 2]}, 60)
            self.assertEqual(await self.cache.aget("k"), {"a": [1, 2]})
            self.assertTrue(await self.cache.ahas_key("k"))
            self.assertFalse(await self.cache.ahas_key("nope"))
            self.assertTrue(await self.cache.adelete("k"))
            self.assertFalse(await self.cache.adelete("k"))
            self.assertIsNone(await self.cache.aget("k"))

    async def test_add_only_replaces_expired_rows(self):
        with no_sync_queries():
            self.assertTrue(await self.cache.aadd("a", 1, 60))
            self.assertFalse(await self.cache.aadd("a", 2, 60))
            self.assertEqual(await self.cache.aget("a"), 1)
            await self.cache.aset("exp", "old", -1)  # already expired
            self.assertTrue(await self.cache.aadd("exp", "new", 60))
            self.assertEqual(await self.cache.aget("exp"), "new")

    async def test_expiry_touch_and_many(self):
        with no_sync_queries():
            await self.cache.aset("short", "v", 1)
            self.assertTrue(await self.cache.atouch("short", 60))
            self.assertFalse(await self.cache.atouch("absent", 60))
            await self.cache.aset("gone", "v", -1)
            self.assertIsNone(await self.cache.aget("gone"))  # expired rows are pruned
            await self.cache.aset_many({"m1": 1, "m2": 2}, 60)
            self.assertEqual(await self.cache.aget_many(["m1", "m2", "m3"]), {"m1": 1, "m2": 2})
            await self.cache.adelete_many(["m1", "m2"])
            self.assertEqual(await self.cache.aget_many(["m1", "m2"]), {})

    async def test_composed_helpers(self):
        with no_sync_queries():
            self.assertEqual(await self.cache.aget_or_set("gs", lambda: 41, 60), 41)
            self.assertEqual(await self.cache.aget_or_set("gs", 0, 60), 41)
            self.assertEqual(await self.cache.aincr("gs"), 42)
            self.assertEqual(await self.cache.adecr("gs", 2), 40)
            with self.assertRaises(ValueError):
                await self.cache.aincr("absent")
            await self.cache.aclear()
            self.assertIsNone(await self.cache.aget("gs"))

    async def test_culling(self):
        # MAX_ENTRIES=50, CULL_FREQUENCY=2: past the limit, half the rows go.
        with no_sync_queries():
            for i in range(60):
                await self.cache.aset("cull-%02d" % i, i, 600)
            present = await self.cache.aget_many(["cull-%02d" % i for i in range(60)])
        self.assertLess(len(present), 60)
        self.assertGreater(len(present), 20)

    async def test_writes_join_the_surrounding_transaction(self):
        with no_sync_queries():
            try:
                async with async_atomic():
                    await self.cache.aset("tx", 1, 60)
                    self.assertEqual(await self.cache.aget("tx"), 1)
                    raise RuntimeError("roll back")
            except RuntimeError:
                pass
            self.assertIsNone(await self.cache.aget("tx"))

    async def test_sync_methods_still_work(self):
        from asgiref.sync import sync_to_async

        try:
            await sync_to_async(self.cache.set)("sync", 1, 60)
            self.assertEqual(await sync_to_async(self.cache.get)("sync"), 1)
        finally:
            await _close_sync_connections()


@override_settings(SESSION_ENGINE="django.contrib.sessions.backends.cache")
class CacheSessionTests(AsyncioTransactionTestCase):
    """Django's cache session store is async by composition once the cache is."""

    async def asyncSetUp(self):
        self.client = AsyncClient()
        user = User(username="root", is_staff=True, is_superuser=True)
        user.set_password("pw")
        await user.async_save()

    async def asyncTearDown(self):
        await _close_sync_connections()
        await caches["default"].aclear()
        await User.async_objects.all().adelete()
        await Session.async_objects.all().adelete()

    async def test_login_and_browse_on_cache_sessions(self):
        with no_sync_queries():
            r = await self.client.post("/admin/login/", {"username": "root", "password": "pw"})
            self.assertEqual(r.status_code, 302, r.content[:200])
            r = await self.client.get("/admin/")
            self.assertEqual(r.status_code, 200)
            self.assertIn(b"root", r.content)
            r = await self.client.post("/admin/logout/")
            self.assertEqual(r.status_code, 200)
            r = await self.client.get("/admin/")
            self.assertEqual(r.status_code, 302)


class CachePageTests(AsyncioTransactionTestCase):
    async def asyncTearDown(self):
        await _close_sync_connections()
        await caches["default"].aclear()

    async def test_acache_page_serves_second_request_from_cache(self):
        from admin_urls import CACHED_VIEW_HITS

        CACHED_VIEW_HITS.clear()
        client = AsyncClient()
        with no_sync_queries():
            r1 = await client.get("/cached/?q=1")
            r2 = await client.get("/cached/?q=1")
            r3 = await client.get("/cached/?q=2")
        self.assertEqual((r1.status_code, r2.status_code, r3.status_code), (200, 200, 200))
        self.assertEqual(r1.content, r2.content)
        self.assertEqual(len(CACHED_VIEW_HITS), 2)  # q=1 computed once, q=2 once
        self.assertIn("max-age=30", r1["Cache-Control"])
        self.assertIn("Age", r2.headers)
        await asyncio.sleep(0)
