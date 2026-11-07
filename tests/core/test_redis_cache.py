"""AsyncRedisCache: every ``a*`` method on redis.asyncio, no worker thread."""

import asyncio
import contextlib
import unittest
from unittest import mock

import asgiref.sync
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.cached_db import KEY_PREFIX
from django.contrib.sessions.models import Session
from django.core.cache import caches
from django.test import AsyncClient

from django_async_backend.contrib.sessions.cached_db import SessionStore
from django_async_backend.core.cache.redis import AsyncRedisCache
from django_async_backend.test import (
    AsyncioTestCase,
    AsyncioTransactionTestCase,
    override_settings,
)
from tests.contrib.test_admin import (
    _close_sync_connections,
    no_sync_queries,
)

User = get_user_model()


def _redis_available():
    try:
        import redis

        redis.Redis.from_url(
            settings.CACHES["redis"]["LOCATION"], socket_connect_timeout=1
        ).ping()
    except Exception:
        return False
    return True


REDIS = _redis_available()
needs_redis = unittest.skipUnless(REDIS, "no Redis at CACHES['redis']")


@contextlib.contextmanager
def no_thread_hops():
    """Fail if anything goes through sync_to_async() while active."""

    def __call__(self, *args, **kwargs):
        raise AssertionError("sync_to_async() was used: %r" % (self.func,))

    with mock.patch.object(asgiref.sync.SyncToAsync, "__call__", __call__):
        yield


@needs_redis
class AsyncRedisCacheTests(AsyncioTestCase):
    def setUp(self):
        self.cache = caches["redis"]
        self.assertIsInstance(self.cache, AsyncRedisCache)

    async def asyncSetUp(self):
        await self.cache.aclear()

    async def test_round_trip_and_defaults(self):
        with no_thread_hops(), no_sync_queries():
            self.assertIsNone(await self.cache.aget("missing"))
            self.assertEqual(await self.cache.aget("missing", "dflt"), "dflt")
            await self.cache.aset("k", {"a": [1, 2]}, 60)
            self.assertEqual(await self.cache.aget("k"), {"a": [1, 2]})
            self.assertTrue(await self.cache.ahas_key("k"))
            self.assertFalse(await self.cache.ahas_key("nope"))
            self.assertTrue(await self.cache.adelete("k"))
            self.assertFalse(await self.cache.adelete("k"))
            self.assertIsNone(await self.cache.aget("k"))

    async def test_add_only_when_absent(self):
        with no_thread_hops():
            self.assertTrue(await self.cache.aadd("a", 1, 60))
            self.assertFalse(await self.cache.aadd("a", 2, 60))
            self.assertEqual(await self.cache.aget("a"), 1)
            # timeout=0: the key is never stored but add() reports the
            # outcome, as in Django's backend.
            self.assertTrue(await self.cache.aadd("zero", 1, 0))
            self.assertFalse(await self.cache.ahas_key("zero"))

    async def test_expiry_touch_and_many(self):
        with no_thread_hops():
            await self.cache.aset("short", "v", 1)
            self.assertTrue(await self.cache.atouch("short", 60))
            self.assertFalse(await self.cache.atouch("absent", 60))
            await self.cache.aset("gone", "v", 1)
            await self.cache.aset("forever", "v", None)
            await self.cache.aset_many({"m1": 1, "m2": 2}, 60)
            self.assertEqual(
                await self.cache.aget_many(["m1", "m2", "m3"]),
                {"m1": 1, "m2": 2},
            )
            await self.cache.adelete_many(["m1", "m2"])
            self.assertEqual(await self.cache.aget_many(["m1", "m2"]), {})
            await self.cache.aset("zero", "v", 0)  # deleted at once
            self.assertFalse(await self.cache.ahas_key("zero"))
        await asyncio.sleep(1.1)
        with no_thread_hops():
            self.assertIsNone(await self.cache.aget("gone"))
            self.assertEqual(await self.cache.aget("short"), "v")
            self.assertEqual(await self.cache.aget("forever"), "v")

    async def test_composed_helpers(self):
        with no_thread_hops():
            self.assertEqual(
                await self.cache.aget_or_set("gs", lambda: 41, 60), 41
            )
            self.assertEqual(await self.cache.aget_or_set("gs", 0, 60), 41)
            self.assertEqual(await self.cache.aincr("gs"), 42)
            self.assertEqual(await self.cache.adecr("gs", 2), 40)
            with self.assertRaises(ValueError):
                await self.cache.aincr("absent")
            self.assertEqual(await self.cache.aincr_version("gs"), 2)
            self.assertEqual(await self.cache.aget("gs", version=2), 40)
            self.assertIsNone(await self.cache.aget("gs"))
            await self.cache.aclear()
            self.assertIsNone(await self.cache.aget("gs", version=2))

    async def test_sync_methods_still_work(self):
        from asgiref.sync import sync_to_async

        try:
            await sync_to_async(self.cache.set)("sync", 1, 60)
            self.assertEqual(await sync_to_async(self.cache.get)("sync"), 1)
            # Both clients speak to the same keys.
            self.assertEqual(await self.cache.aget("sync"), 1)
        finally:
            await _close_sync_connections()

    async def test_pools_are_per_event_loop(self):
        await self.cache.aset("loop", "main", 60)
        client = self.cache._acache
        main_loop = asyncio.get_running_loop()
        self.assertIn(main_loop, client._pools)

        def in_another_loop():
            return asyncio.run(self.cache.aget("loop"))

        # A fresh loop in another thread gets its own pool; sharing the
        # main loop's connections would fail with "attached to a different
        # loop".
        self.assertEqual(await asyncio.to_thread(in_another_loop), "main")
        self.assertIn(main_loop, client._pools)

    async def test_adisconnect_releases_the_loop_pools(self):
        await self.cache.aset("d", 1, 60)
        client = self.cache._acache
        await self.cache.adisconnect()
        self.assertNotIn(asyncio.get_running_loop(), client._pools)
        # And reconnects on the next call.
        self.assertEqual(await self.cache.aget("d"), 1)

    async def test_options_are_split_between_the_clients(self):
        import redis
        import redis.asyncio as aredis

        cache = AsyncRedisCache(
            "redis://localhost:1/0",
            {
                "OPTIONS": {
                    "socket_timeout": 3,
                    "pool_class": "redis.BlockingConnectionPool",
                    "async_pool_class": "redis.asyncio.BlockingConnectionPool",
                }
            },
        )
        self.assertEqual(
            cache._options,
            {
                "socket_timeout": 3,
                "pool_class": "redis.BlockingConnectionPool",
            },
        )
        self.assertIs(cache._cache._pool_class, redis.BlockingConnectionPool)
        self.assertIs(cache._acache._pool_class, aredis.BlockingConnectionPool)
        self.assertEqual(cache._acache._pool_options["socket_timeout"], 3)


@needs_redis
@override_settings(
    SESSION_ENGINE="django_async_backend.contrib.sessions.cached_db",
    SESSION_CACHE_ALIAS="redis",
)
class CachedDbSessionTests(AsyncioTransactionTestCase):
    """cached_db sessions: Redis in front, the async connection behind."""

    async def asyncSetUp(self):
        self.client = AsyncClient()
        user = User(username="root", is_staff=True, is_superuser=True)
        user.set_password("pw")
        await user.async_save()
        await caches["redis"].aclear()

    async def asyncTearDown(self):
        await _close_sync_connections()
        await caches["redis"].aclear()
        await User.async_objects.all().adelete()
        await Session.async_objects.all().adelete()

    async def test_login_and_browse_on_cached_db_sessions(self):
        with no_sync_queries():
            r = await self.client.post(
                "/admin/login/", {"username": "root", "password": "pw"}
            )
            self.assertEqual(r.status_code, 302, r.content[:200])
            session_key = self.client.cookies[
                settings.SESSION_COOKIE_NAME
            ].value
            # Written through: the row is in the database and the cache.
            self.assertTrue(
                await Session.async_objects.filter(
                    session_key=session_key
                ).aexists()
            )
            self.assertTrue(
                await caches["redis"].ahas_key(KEY_PREFIX + session_key)
            )
            r = await self.client.get("/admin/")
            self.assertEqual(r.status_code, 200)
            self.assertIn(b"root", r.content)
            r = await self.client.post("/admin/logout/")
            self.assertEqual(r.status_code, 200)
            self.assertFalse(
                await caches["redis"].ahas_key(KEY_PREFIX + session_key)
            )
            r = await self.client.get("/admin/")
            self.assertEqual(r.status_code, 302)

    async def test_cache_miss_falls_back_to_the_database(self):
        with no_sync_queries():
            store = SessionStore()
            store["x"] = 1
            await store.asave()
            key = store.session_key
            await caches["redis"].adelete(KEY_PREFIX + key)

            reloaded = SessionStore(key)
            self.assertEqual(await reloaded.aget("x"), 1)
            # ... and the cache is primed again by the load.
            self.assertTrue(await caches["redis"].ahas_key(KEY_PREFIX + key))

    async def test_aexists_answers_from_the_cache_first(self):
        with no_sync_queries():
            store = SessionStore()
            await store.acreate()
            key = store.session_key
            await Session.async_objects.filter(session_key=key).adelete()
            self.assertTrue(await SessionStore().aexists(key))
            await caches["redis"].adelete(KEY_PREFIX + key)
            self.assertFalse(await SessionStore().aexists(key))
