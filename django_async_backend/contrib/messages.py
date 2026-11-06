"""``MessageMiddleware`` with a native async request path.

Django's middleware only has sync hooks. Its ``process_response`` calls
``storage.update()``, which with a session-backed storage writes to
``request.session``; on a request that never loaded the session that is a
synchronous load, on a worker thread. This subclass keeps the sync path and
adds an ``__acall__`` that loads the session on the async connection first,
so ``update()`` only touches the session cache (the async session middleware
then saves it)::

    MIDDLEWARE = [
        ...,
        "django_async_backend.contrib.sessions.AsyncSessionMiddleware",
        ...,
        "django_async_backend.contrib.messages.AsyncMessageMiddleware",
    ]
"""

from django.conf import settings
from django.contrib.messages.middleware import (
    MessageMiddleware as DjangoMessageMiddleware,
)
from django.contrib.messages.storage import default_storage


class AsyncMessageMiddleware(DjangoMessageMiddleware):
    async def __acall__(self, request):
        request._messages = default_storage(request)
        response = await self.get_response(request)
        if hasattr(request, "_messages"):
            session = getattr(request, "session", None)
            if session is not None and hasattr(session, "_aget_session"):
                # Make the later sync session reads/writes cache hits.
                await session._aget_session()
            unstored_messages = request._messages.update(response)
            if unstored_messages and settings.DEBUG:
                raise ValueError("Not all temporary messages could be stored.")
        return response
