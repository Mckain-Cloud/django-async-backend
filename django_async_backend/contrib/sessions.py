"""Database session store on the async connection.

Django's ``SessionStore.asave()`` wraps the write in a *sync* ``atomic()``
through ``sync_to_async``, so every session write of an async request goes to
a thread and a second connection. This store runs the same logic with
``async_atomic()`` and ``async_save()``::

    SESSION_ENGINE = "django_async_backend.contrib.sessions"

The sync methods are inherited unchanged, so sync views keep working.
"""

import logging

from django.contrib.sessions.backends.base import (
    CreateError,
    UpdateError,
)
from django.contrib.sessions.backends.db import SessionStore as DbSessionStore
from django.core.exceptions import SuspiciousOperation
from django.db import (
    DatabaseError,
    IntegrityError,
    router,
)
from django.utils import timezone

from django_async_backend.db.transaction import async_atomic


class SessionStore(DbSessionStore):
    async def _aget_session_from_db(self):
        try:
            return await self.model.async_objects.aget(
                session_key=self.session_key, expire_date__gt=timezone.now()
            )
        except (self.model.DoesNotExist, SuspiciousOperation) as e:
            if isinstance(e, SuspiciousOperation):
                logger = logging.getLogger(
                    "django.security.%s" % e.__class__.__name__
                )
                logger.warning(str(e))
            self._session_key = None

    async def aexists(self, session_key):
        return await self.model.async_objects.filter(
            session_key=session_key
        ).aexists()

    async def asave(self, must_create=False):
        if self.session_key is None:
            return await self.acreate()
        data = await self._aget_session(no_load=must_create)
        obj = await self.acreate_model_instance(data)
        using = router.db_for_write(self.model, instance=obj)
        try:
            async with async_atomic(using=using):
                await obj.async_save(
                    force_insert=must_create,
                    force_update=not must_create,
                    using=using,
                )
        except IntegrityError:
            if must_create:
                raise CreateError
            raise
        except DatabaseError:
            if not must_create:
                raise UpdateError
            raise

    async def adelete(self, session_key=None):
        if session_key is None:
            if self.session_key is None:
                return
            session_key = self.session_key
        await self.model.async_objects.filter(
            session_key=session_key
        ).adelete()

    @classmethod
    async def aclear_expired(cls):
        await cls.get_model_class().async_objects.filter(
            expire_date__lt=timezone.now()
        ).adelete()
