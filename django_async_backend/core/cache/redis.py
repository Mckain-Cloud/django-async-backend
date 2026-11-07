"""Redis cache backend whose ``a*`` methods run on ``redis.asyncio``.

Django's ``RedisCache`` inherits every ``a*`` method from ``BaseCache``, where
each one is ``sync_to_async`` over the sync client: a worker thread per cache
call. ``AsyncRedisCache`` is Django's backend with the async methods served by
``redis.asyncio`` on the running event loop, so ``cached_db`` / ``cache``
sessions, ``{% cache %}``, ``acache_page()``, REST framework throttles and
``cache.aadd()`` locks stay in the request's task. The sync methods are
Django's own, so sync views, Celery workers and management commands keep
working::

    CACHES = {
        "default": {
            "BACKEND": "django_async_backend.core.cache.redis.AsyncRedisCache",
            "LOCATION": "redis://redis:6379/0",
            "OPTIONS": {"socket_connect_timeout": 5, "socket_timeout": 5},
        }
    }

``OPTIONS`` go to both clients' connection pools, as in Django's backend.
``pool_class`` / ``parser_class`` select the sync pool and parser;
``async_pool_class`` / ``async_parser_class`` the async ones.

``redis.asyncio`` connections belong to the event loop that opened them, so
the async client keeps one pool per loop (one loop per worker process in
production, one per test class under ``AsyncioTestCase``). A pool lives as
long as its loop, or until ``await cache.adisconnect()``.
"""

import asyncio
import random
import threading
import weakref

import django
from django.core.cache.backends.base import DEFAULT_TIMEOUT
from django.core.cache.backends.redis import (
    RedisCache,
    RedisSerializer,
)
from django.utils.functional import cached_property
from django.utils.module_loading import import_string

__all__ = ["AsyncRedisCache", "AsyncRedisCacheClient"]

_SYNC_ONLY_OPTIONS = ("pool_class", "parser_class")
_ASYNC_ONLY_OPTIONS = ("async_pool_class", "async_parser_class")


class AsyncRedisCacheClient:
    """Django's ``RedisCacheClient`` over ``redis.asyncio``."""

    def __init__(
        self,
        servers,
        serializer=None,
        pool_class=None,
        parser_class=None,
        **options,
    ):
        import redis
        import redis.asyncio as aredis

        self._lib = aredis
        self._servers = servers
        # event loop -> {server index: pool}
        self._pools = weakref.WeakKeyDictionary()
        self._lock = threading.Lock()

        self._client = aredis.Redis

        if isinstance(pool_class, str):
            pool_class = import_string(pool_class)
        self._pool_class = pool_class or aredis.ConnectionPool

        if isinstance(serializer, str):
            serializer = import_string(serializer)
        if callable(serializer):
            serializer = serializer()
        self._serializer = serializer or RedisSerializer()

        if isinstance(parser_class, str):
            parser_class = import_string(parser_class)
        parser_class = parser_class or aredis.connection.DefaultParser

        version = django.get_version()
        if hasattr(redis, "DriverInfo"):
            driver_info = redis.DriverInfo().add_upstream_driver(
                "django", version
            )
            driver_info_options = {"driver_info": driver_info}
        else:  # pragma: no cover - older redis-py
            driver_info_options = {"lib_name": f"redis-py(django_v{version})"}

        self._pool_options = {
            "parser_class": parser_class,
            **driver_info_options,
            **options,
        }

    def _get_connection_pool_index(self, write):
        # Write to the first server. Read from other servers if there are
        # more, otherwise read from the first server.
        if write or len(self._servers) == 1:
            return 0
        return random.randint(1, len(self._servers) - 1)  # nosec B311

    def _get_connection_pool(self, write):
        index = self._get_connection_pool_index(write)
        loop = asyncio.get_running_loop()
        with self._lock:
            pools = self._pools.get(loop)
            if pools is None:
                pools = self._pools[loop] = {}
            pool = pools.get(index)
            if pool is None:
                pool = pools[index] = self._pool_class.from_url(
                    self._servers[index],
                    **self._pool_options,
                )
        return pool

    def get_client(self, key=None, *, write=False):
        # key is unused, as in Django: kept so a sharding client can use it.
        pool = self._get_connection_pool(write)
        return self._client(connection_pool=pool)

    async def add(self, key, value, timeout):
        client = self.get_client(key, write=True)
        value = self._serializer.dumps(value)

        if timeout == 0:
            if ret := bool(await client.set(key, value, nx=True)):
                await client.delete(key)
            return ret
        return bool(await client.set(key, value, ex=timeout, nx=True))

    async def get(self, key, default):
        client = self.get_client(key)
        value = await client.get(key)
        return default if value is None else self._serializer.loads(value)

    async def set(self, key, value, timeout):
        client = self.get_client(key, write=True)
        value = self._serializer.dumps(value)
        if timeout == 0:
            await client.delete(key)
        else:
            await client.set(key, value, ex=timeout)

    async def touch(self, key, timeout):
        client = self.get_client(key, write=True)
        if timeout is None:
            return bool(await client.persist(key))
        return bool(await client.expire(key, timeout))

    async def delete(self, key):
        client = self.get_client(key, write=True)
        return bool(await client.delete(key))

    async def get_many(self, keys):
        client = self.get_client(None)
        ret = await client.mget(keys)
        return {
            k: self._serializer.loads(v)
            for k, v in zip(keys, ret)
            if v is not None
        }

    async def has_key(self, key):
        client = self.get_client(key)
        return bool(await client.exists(key))

    async def incr(self, key, delta):
        client = self.get_client(key, write=True)
        if not await client.exists(key):
            raise ValueError("Key '%s' not found." % key)
        return await client.incr(key, delta)

    async def set_many(self, data, timeout):
        client = self.get_client(None, write=True)
        async with client.pipeline() as pipeline:
            pipeline.mset(
                {k: self._serializer.dumps(v) for k, v in data.items()}
            )
            if timeout is not None:
                # Setting timeout for each key as redis does not support
                # timeout with mset().
                for key in data:
                    pipeline.expire(key, timeout)
            await pipeline.execute()

    async def delete_many(self, keys):
        client = self.get_client(None, write=True)
        await client.delete(*keys)

    async def clear(self):
        client = self.get_client(None, write=True)
        return bool(await client.flushdb())

    async def disconnect(self):
        """Close the pools that belong to the running event loop."""
        loop = asyncio.get_running_loop()
        with self._lock:
            pools = self._pools.pop(loop, None) or {}
        for pool in pools.values():
            await pool.disconnect()


class AsyncRedisCache(RedisCache):
    _aclass = AsyncRedisCacheClient

    def __init__(self, server, params):
        options = dict(params.get("OPTIONS") or {})
        async_only = {
            name[len("async_") :]: options.pop(name)
            for name in _ASYNC_ONLY_OPTIONS
            if name in options
        }
        super().__init__(server, {**params, "OPTIONS": options})
        shared = {
            name: value
            for name, value in options.items()
            if name not in _SYNC_ONLY_OPTIONS
        }
        self._aoptions = {**shared, **async_only}

    @cached_property
    def _acache(self):
        return self._aclass(self._servers, **self._aoptions)

    async def aadd(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        key = self.make_and_validate_key(key, version=version)
        return await self._acache.add(
            key, value, self.get_backend_timeout(timeout)
        )

    async def aget(self, key, default=None, version=None):
        key = self.make_and_validate_key(key, version=version)
        return await self._acache.get(key, default)

    async def aset(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        key = self.make_and_validate_key(key, version=version)
        await self._acache.set(key, value, self.get_backend_timeout(timeout))

    async def atouch(self, key, timeout=DEFAULT_TIMEOUT, version=None):
        key = self.make_and_validate_key(key, version=version)
        return await self._acache.touch(key, self.get_backend_timeout(timeout))

    async def adelete(self, key, version=None):
        key = self.make_and_validate_key(key, version=version)
        return await self._acache.delete(key)

    async def aget_many(self, keys, version=None):
        key_map = {
            self.make_and_validate_key(key, version=version): key
            for key in keys
        }
        ret = await self._acache.get_many(key_map.keys())
        return {key_map[k]: v for k, v in ret.items()}

    async def ahas_key(self, key, version=None):
        key = self.make_and_validate_key(key, version=version)
        return await self._acache.has_key(key)

    async def aincr(self, key, delta=1, version=None):
        key = self.make_and_validate_key(key, version=version)
        return await self._acache.incr(key, delta)

    async def aset_many(self, data, timeout=DEFAULT_TIMEOUT, version=None):
        if not data:
            return []
        safe_data = {}
        for key, value in data.items():
            key = self.make_and_validate_key(key, version=version)
            safe_data[key] = value
        await self._acache.set_many(
            safe_data, self.get_backend_timeout(timeout)
        )
        return []

    async def adelete_many(self, keys, version=None):
        if not keys:
            return
        safe_keys = [
            self.make_and_validate_key(key, version=version) for key in keys
        ]
        await self._acache.delete_many(safe_keys)

    async def aclear(self):
        return await self._acache.clear()

    async def aclose(self, **kwargs):
        # Pools outlive requests, as the sync pools do; close() is a no-op in
        # Django's backend and this keeps it off the worker thread.
        pass

    async def adisconnect(self):
        """Close the running loop's pools (test teardown, worker exit)."""
        if "_acache" in self.__dict__:
            await self._acache.disconnect()

    # BaseCache composes aget_or_set, adecr, aincr_version and adecr_version
    # from the methods above.
