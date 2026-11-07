"""A database cache backend, page cache and ``cache_page`` on the async
connection.

Django's ``BaseCache`` implements every ``a*`` method as a ``sync_to_async``
hop, so cache-backed sessions, ``{% cache %}``, ``cache_page`` and REST
framework throttles all block a worker thread per call. ``AsyncDatabaseCache``
is Django's ``DatabaseCache`` with the async methods implemented on the
request's async connection (PostgreSQL ``INSERT ... ON CONFLICT`` instead of
Django's select-then-insert-or-update). The sync methods are untouched, so
``createcachetable`` and sync code keep working.

Because the rows live in the request's database and the operations run on its
connection, a cache write inside ``async_atomic()`` is rolled back with the
transaction, like Django's own ``DatabaseCache`` under ``ATOMIC_REQUESTS``.

``AsyncCacheMiddleware`` / ``acache_page()`` are the async twins of Django's
``CacheMiddleware`` / ``cache_page()``; Django's helpers ``get_cache_key()``
and ``learn_cache_key()`` call ``cache.get()``/``cache.set()`` directly, so
they are mirrored here as ``aget_cache_key()`` / ``alearn_cache_key()``.
"""

import base64
import pickle
import time
from datetime import UTC, datetime
from functools import wraps

from django.conf import settings
from django.core.cache import caches
from django.core.cache.backends.base import DEFAULT_TIMEOUT
from django.core.cache.backends.db import DatabaseCache
from django.db import DatabaseError, models, router
from django.middleware.cache import CacheMiddleware
from django.utils.cache import (
    _generate_cache_header_key,
    _generate_cache_key,
    get_max_age,
    has_vary_header,
    patch_response_headers,
    patch_vary_headers,
)
from django.utils.http import (
    parse_http_date_safe,
    split_directive_names,
    split_header_value,
)
from django.utils.timezone import now as tz_now

from django_async_backend.db import async_connections
from django_async_backend.db.transaction import async_atomic

__all__ = [
    "AsyncCacheMiddleware",
    "AsyncDatabaseCache",
    "acache_page",
    "aget_cache_key",
    "alearn_cache_key",
]


class AsyncDatabaseCache(DatabaseCache):
    def _aconnection(self, write=False):
        db = (router.db_for_write if write else router.db_for_read)(self.cache_model_class)
        return db, async_connections[db]

    def _expires_converters(self, connection):
        expression = models.Expression(output_field=models.DateTimeField())
        converters = connection.ops.get_db_converters(expression)
        converters += expression.get_db_converters(connection)
        return expression, converters

    # ---- reads -------------------------------------------------------------

    async def aget(self, key, default=None, version=None):
        return (await self.aget_many([key], version)).get(key, default)

    async def aget_many(self, keys, version=None):
        if not keys:
            return {}
        key_map = {self.make_and_validate_key(key, version=version): key for key in keys}
        db, connection = self._aconnection()
        quote_name = connection.ops.quote_name
        table = quote_name(self._table)
        async with await connection.cursor() as cursor:
            await cursor.execute(
                "SELECT %s, %s, %s FROM %s WHERE %s IN (%s)"
                % (
                    quote_name("cache_key"),
                    quote_name("value"),
                    quote_name("expires"),
                    table,
                    quote_name("cache_key"),
                    ", ".join(["%s"] * len(key_map)),
                ),
                list(key_map),
            )
            rows = await cursor.fetchall()
        result = {}
        expired_keys = []
        expression, converters = self._expires_converters(connection)
        for key, value, expires in rows:
            for converter in converters:
                expires = converter(expires, expression, connection)
            if expires < tz_now():
                expired_keys.append(key)
            else:
                value = connection.ops.process_clob(value)
                value = pickle.loads(base64.b64decode(value.encode(), validate=True))
                result[key_map.get(key)] = value
        await self._abase_delete_many(expired_keys)
        return result

    async def ahas_key(self, key, version=None):
        key = self.make_and_validate_key(key, version=version)
        db, connection = self._aconnection()
        quote_name = connection.ops.quote_name
        now = tz_now().replace(microsecond=0, tzinfo=None)
        async with await connection.cursor() as cursor:
            await cursor.execute(
                "SELECT %s FROM %s WHERE %s = %%s and %s > %%s"
                % (
                    quote_name("cache_key"),
                    quote_name(self._table),
                    quote_name("cache_key"),
                    quote_name("expires"),
                ),
                [key, connection.ops.adapt_datetimefield_value(now)],
            )
            return await cursor.fetchone() is not None

    # ---- writes ------------------------------------------------------------

    async def aset(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        key = self.make_and_validate_key(key, version=version)
        await self._abase_set("set", key, value, timeout)

    async def aadd(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        key = self.make_and_validate_key(key, version=version)
        return await self._abase_set("add", key, value, timeout)

    async def atouch(self, key, timeout=DEFAULT_TIMEOUT, version=None):
        key = self.make_and_validate_key(key, version=version)
        return await self._abase_set("touch", key, None, timeout)

    async def aset_many(self, data, timeout=DEFAULT_TIMEOUT, version=None):
        for key, value in data.items():
            await self.aset(key, value, timeout=timeout, version=version)
        return []

    def _expiry(self, timeout):
        timeout = self.get_backend_timeout(timeout)
        if timeout is None:
            exp = datetime.max
        else:
            exp = datetime.fromtimestamp(timeout, tz=UTC if settings.USE_TZ else None)
        return exp.replace(microsecond=0)

    async def _abase_set(self, mode, key, value, timeout=DEFAULT_TIMEOUT):
        db, connection = self._aconnection(write=True)
        quote_name = connection.ops.quote_name
        table = quote_name(self._table)
        now = tz_now().replace(microsecond=0)
        exp = connection.ops.adapt_datetimefield_value(self._expiry(timeout))
        try:
            async with async_atomic(using=db):
                async with await connection.cursor() as cursor:
                    await cursor.execute("SELECT COUNT(*) FROM %s" % table)
                    num = (await cursor.fetchone())[0]
                    if num > self._max_entries:
                        await self._acull(db, connection, cursor, now, num)
                    if mode == "touch":
                        await cursor.execute(
                            "UPDATE %s SET %s = %%s WHERE %s = %%s"
                            % (table, quote_name("expires"), quote_name("cache_key")),
                            [exp, key],
                        )
                        return bool(cursor.rowcount)
                    pickled = pickle.dumps(value, self.pickle_protocol)
                    b64encoded = base64.b64encode(pickled).decode("latin1")
                    columns = "%s, %s, %s" % (
                        quote_name("cache_key"),
                        quote_name("value"),
                        quote_name("expires"),
                    )
                    upsert = (
                        "INSERT INTO %s (%s) VALUES (%%s, %%s, %%s) "
                        "ON CONFLICT (%s) DO UPDATE SET %s = EXCLUDED.%s, %s = EXCLUDED.%s"
                        % (
                            table,
                            columns,
                            quote_name("cache_key"),
                            quote_name("value"),
                            quote_name("value"),
                            quote_name("expires"),
                            quote_name("expires"),
                        )
                    )
                    params = [key, b64encoded, exp]
                    if mode == "add":
                        # Only replace an existing row when it has expired.
                        upsert += " WHERE %s.%s < %%s" % (table, quote_name("expires"))
                        params.append(connection.ops.adapt_datetimefield_value(now))
                    await cursor.execute(upsert, params)
                    return bool(cursor.rowcount)
        except DatabaseError:
            # Parity with DatabaseCache: writes may fail silently.
            return False

    async def _acull(self, db, connection, cursor, now, num):
        if self._cull_frequency == 0:
            await self.aclear()
            return
        table = connection.ops.quote_name(self._table)
        await cursor.execute(
            "DELETE FROM %s WHERE %s < %%s" % (table, connection.ops.quote_name("expires")),
            [connection.ops.adapt_datetimefield_value(now)],
        )
        remaining_num = num - cursor.rowcount
        if remaining_num > self._max_entries:
            cull_num = remaining_num // self._cull_frequency
            await cursor.execute(connection.ops.cache_key_culling_sql() % table, [cull_num])
            last_cache_key = await cursor.fetchone()
            if last_cache_key:
                await cursor.execute(
                    "DELETE FROM %s WHERE %s < %%s"
                    % (table, connection.ops.quote_name("cache_key")),
                    [last_cache_key[0]],
                )

    async def adelete(self, key, version=None):
        key = self.make_and_validate_key(key, version=version)
        return await self._abase_delete_many([key])

    async def adelete_many(self, keys, version=None):
        keys = [self.make_and_validate_key(key, version=version) for key in keys]
        await self._abase_delete_many(keys)

    async def _abase_delete_many(self, keys):
        if not keys:
            return False
        db, connection = self._aconnection(write=True)
        quote_name = connection.ops.quote_name
        async with await connection.cursor() as cursor:
            await cursor.execute(
                "DELETE FROM %s WHERE %s IN (%s)"
                % (
                    quote_name(self._table),
                    quote_name("cache_key"),
                    ", ".join(["%s"] * len(keys)),
                ),
                keys,
            )
            return bool(cursor.rowcount)

    async def aclear(self):
        db, connection = self._aconnection(write=True)
        async with await connection.cursor() as cursor:
            await cursor.execute("DELETE FROM %s" % connection.ops.quote_name(self._table))

    async def aclose(self, **kwargs):
        pass

    # BaseCache's aincr/adecr/aget_or_set/aincr_version/adecr_version are
    # composed from the methods above and need no override.


# ------------------------------------------------------------ page cache ----


async def aget_cache_key(request, key_prefix=None, method="GET", cache=None):
    """Async mirror of ``django.utils.cache.get_cache_key()``."""
    if key_prefix is None:
        key_prefix = settings.CACHE_MIDDLEWARE_KEY_PREFIX
    cache_key = _generate_cache_header_key(key_prefix, request)
    if cache is None:
        cache = caches[settings.CACHE_MIDDLEWARE_ALIAS]
    headerlist = await cache.aget(cache_key)
    if headerlist is not None:
        return _generate_cache_key(request, method, headerlist, key_prefix)
    return None


async def alearn_cache_key(request, response, cache_timeout=None, key_prefix=None, cache=None):
    """Async mirror of ``django.utils.cache.learn_cache_key()``."""
    if key_prefix is None:
        key_prefix = settings.CACHE_MIDDLEWARE_KEY_PREFIX
    if cache_timeout is None:
        cache_timeout = settings.CACHE_MIDDLEWARE_SECONDS
    cache_key = _generate_cache_header_key(key_prefix, request)
    if cache is None:
        cache = caches[settings.CACHE_MIDDLEWARE_ALIAS]
    if response.has_header("Vary"):
        is_accept_language_redundant = settings.USE_I18N
        headerlist = []
        for header in split_header_value(response.headers["Vary"]):
            header = header.upper().replace("-", "_")
            if header != "ACCEPT_LANGUAGE" or not is_accept_language_redundant:
                headerlist.append("HTTP_" + header)
        headerlist.sort()
        await cache.aset(cache_key, headerlist, cache_timeout)
        return _generate_cache_key(request, request.method, headerlist, key_prefix)
    await cache.aset(cache_key, [], cache_timeout)
    return _generate_cache_key(request, request.method, [], key_prefix)


class AsyncCacheMiddleware(CacheMiddleware):
    """``CacheMiddleware`` whose request and response phases await the cache.

    A not-yet-rendered ``TemplateResponse`` is rendered here before being
    stored (the async template engine is used when it is configured), so the
    store never falls back to a sync post-render callback.
    """

    async def __acall__(self, request):
        response = await self.aprocess_request(request)
        if response is None:
            response = await self.get_response(request)
        return await self.aprocess_response(request, response)

    async def aprocess_request(self, request):
        if request.method not in ("GET", "HEAD"):
            request._cache_update_cache = False
            return None
        cache_key = await aget_cache_key(request, self.key_prefix, "GET", cache=self.cache)
        if cache_key is None:
            request._cache_update_cache = True
            return None
        response = await self.cache.aget(cache_key)
        if response is None and request.method == "HEAD":
            cache_key = await aget_cache_key(request, self.key_prefix, "HEAD", cache=self.cache)
            response = await self.cache.aget(cache_key)
        if response is None:
            request._cache_update_cache = True
            return None
        if (max_age_seconds := get_max_age(response)) is not None and (
            expires_timestamp := parse_http_date_safe(response["Expires"])
        ) is not None:
            remaining_seconds = expires_timestamp - int(time.time())
            response["Age"] = max(0, max_age_seconds - remaining_seconds)
        request._cache_update_cache = False
        return response

    async def aprocess_response(self, request, response):
        if not self._should_update_cache(request, response):
            return response
        if response.streaming or response.status_code not in (200, 304):
            return response
        if response.cookies and has_vary_header(response, "Cookie"):
            return response
        cache_control_parts = set(split_directive_names(response.get("Cache-Control", "")))
        if cache_control_parts.intersection({"private", "no-cache", "no-store"}):
            return response
        if has_vary_header(response, "*"):
            return response
        timeout = self.page_timeout
        if timeout is None:
            timeout = get_max_age(response)
            if timeout is None:
                timeout = self.cache_timeout
            elif timeout == 0:
                return response
        patch_response_headers(response, timeout)
        if request.headers.get("Authorization") and "public" not in cache_control_parts:
            patch_vary_headers(response, ("Authorization",))
        if timeout and response.status_code == 200:
            cache_key = await alearn_cache_key(
                request, response, timeout, self.key_prefix, cache=self.cache
            )
            if hasattr(response, "render") and callable(response.render):
                if not response.is_rendered:
                    from django_async_backend.template import arender_response

                    await arender_response(response)
            await self.cache.aset(cache_key, response, timeout)
        return response


def acache_page(timeout, *, cache=None, key_prefix=None):
    """Async ``cache_page()``: serve and store the view's response through
    ``AsyncCacheMiddleware`` on the async cache backend."""

    def decorator(view_func):
        middleware = AsyncCacheMiddleware(
            lambda request: None,
            page_timeout=timeout,
            cache_alias=cache,
            key_prefix=key_prefix,
        )

        @wraps(view_func)
        async def _wrapped(request, *args, **kwargs):
            response = await middleware.aprocess_request(request)
            if response is not None:
                return response
            response = await view_func(request, *args, **kwargs)
            return await middleware.aprocess_response(request, response)

        return _wrapped

    return decorator
