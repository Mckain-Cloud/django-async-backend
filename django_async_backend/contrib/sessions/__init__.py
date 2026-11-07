"""Database session store on the async connection.

Django's ``SessionStore.asave()`` wraps the write in a *sync* ``atomic()``
through ``sync_to_async``, so every session write of an async request goes to
a thread and a second connection. This store runs the same logic with
``async_atomic()`` and ``async_save()``::

    SESSION_ENGINE = "django_async_backend.contrib.sessions"

The sync methods are inherited unchanged, so sync views keep working.
"""

import logging
import time

from django.conf import settings
from django.contrib.sessions.backends.base import (
    CreateError,
    UpdateError,
)
from django.contrib.sessions.backends.db import SessionStore as DbSessionStore
from django.contrib.sessions.exceptions import SessionInterrupted
from django.contrib.sessions.middleware import (
    SessionMiddleware as DjangoSessionMiddleware,
)
from django.core.exceptions import SuspiciousOperation
from django.db import (
    DatabaseError,
    IntegrityError,
    router,
)
from django.utils import timezone
from django.utils.cache import patch_vary_headers
from django.utils.http import http_date

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


class AsyncSessionMiddleware(DjangoSessionMiddleware):
    """``SessionMiddleware`` with a native async request path.

    Django's middleware only has sync ``process_request``/``process_response``
    hooks, so under ASGI the ``session.save()`` at the end of every request
    that touched the session runs in a thread on a sync connection. This
    subclass keeps the sync path (for sync views) and adds an ``__acall__``
    that saves with ``asave()`` on the async connection::

        MIDDLEWARE = [
            ...,
            "django_async_backend.contrib.sessions.AsyncSessionMiddleware",
            ...,
        ]
    """

    async def __acall__(self, request):
        self.process_request(request)  # no query: builds the store only
        response = await self.get_response(request)
        return await self.aprocess_response(request, response)

    async def aprocess_response(self, request, response):
        try:
            accessed = request.session.accessed
            modified = request.session.modified
            empty = request.session.is_empty()
        except AttributeError:
            return response
        if settings.SESSION_COOKIE_NAME in request.COOKIES and empty:
            response.delete_cookie(
                settings.SESSION_COOKIE_NAME,
                path=settings.SESSION_COOKIE_PATH,
                domain=settings.SESSION_COOKIE_DOMAIN,
                samesite=settings.SESSION_COOKIE_SAMESITE,
            )
            need_vary_cookie = True
        else:
            need_vary_cookie = accessed
            if (modified or settings.SESSION_SAVE_EVERY_REQUEST) and not empty:
                if await request.session.aget_expire_at_browser_close():
                    max_age = None
                    expires = None
                else:
                    max_age = await request.session.aget_expiry_age()
                    expires_time = time.time() + max_age
                    expires = http_date(expires_time)
                if response.status_code < 500:
                    try:
                        await request.session.asave()
                    except UpdateError:
                        raise SessionInterrupted(
                            "The request's session was deleted before the "
                            "request completed. The user may have logged "
                            "out in a concurrent request, for example."
                        )
                    response.set_cookie(
                        settings.SESSION_COOKIE_NAME,
                        request.session.session_key,
                        max_age=max_age,
                        expires=expires,
                        domain=settings.SESSION_COOKIE_DOMAIN,
                        path=settings.SESSION_COOKIE_PATH,
                        secure=settings.SESSION_COOKIE_SECURE or None,
                        httponly=settings.SESSION_COOKIE_HTTPONLY or None,
                        samesite=settings.SESSION_COOKIE_SAMESITE,
                    )
                    need_vary_cookie = True
        if need_vary_cookie:
            patch_vary_headers(response, ("Cookie",))
        return response
