"""``cached_db`` sessions: the async cache in front of the async connection.

Django's ``cached_db`` store composes the cache API with the database store,
so with an async cache backend (``AsyncRedisCache``, ``AsyncDatabaseCache``)
and this package's database store every ``a*`` method of the session runs in
the request's task::

    SESSION_ENGINE = "django_async_backend.contrib.sessions.cached_db"
    SESSION_CACHE_ALIAS = "default"

The sync methods are Django's, so sync views keep working.
"""

from django.contrib.sessions.backends.cached_db import (
    SessionStore as CachedDbSessionStore,
)

from django_async_backend.contrib.sessions import (
    SessionStore as AsyncDbSessionStore,
)

__all__ = ["SessionStore"]


class SessionStore(CachedDbSessionStore, AsyncDbSessionStore):
    # Django's cached_db.aexists() tests the cache with ``in`` (the sync
    # has_key()); ask the async cache instead, then the async connection.
    async def aexists(self, session_key):
        if session_key and await self._cache.ahas_key(
            self.cache_key_prefix + session_key
        ):
            return True
        return await AsyncDbSessionStore.aexists(self, session_key)
