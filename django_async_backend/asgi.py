"""
ASGI entry point: Django's handler plus lifespan, lifespan state, pathsend
and websockets.

Django's ``ASGIHandler`` serves HTTP only: a ``lifespan`` scope raises
"Django can only handle ASGI/HTTP connections" (servers log it and carry on
without lifespan), and file responses are read in Python, chunk by chunk.
``get_asgi_application()`` here wraps it::

    # asgi.py
    from django_async_backend.asgi import get_asgi_application

    application = get_asgi_application(
        lifespan="myproject.lifespan.lifespan",   # optional
        websockets=websocket_urlpatterns,          # optional
    )

Lifespan (per worker event loop):

* **Startup** opens the connection pool of every async alias configured
  with ``OPTIONS["pool"]`` and waits for its ``min_size`` connections, so a
  worker that can't reach its database reports ``lifespan.startup.failed``
  instead of failing its first requests. Then the ``lifespan`` hook runs: an
  async context manager factory that receives the lifespan state dict::

      @asynccontextmanager
      async def lifespan(state):
          state["http"] = httpx.AsyncClient()
          yield
          await state["http"].aclose()

* **Shutdown** exits the hook and closes the worker's pools, so Postgres
  sees its connections closed instead of dropped with the process.

* **State**: the server copies the lifespan state into every request scope
  (shallow, so objects created at startup are shared by the worker's
  requests). Views read it as ``request.state``; websocket consumers as
  ``self.scope["state"]``.

Servers that don't send lifespan (granian's ``asginl`` interface) get the
startup run lazily on the worker's first request or websocket, without a
shutdown. Settings:

* ``ASYNC_BACKEND_LIFESPAN``: dotted path of the hook (or pass ``lifespan=``).
* ``ASYNC_BACKEND_LIFESPAN_DATABASES``: ``"pools"`` (default) opens the
  pooled aliases; ``True`` also connects once to each unpooled async alias;
  ``False`` skips the databases.
* ``ASYNC_BACKEND_PATHSEND``: ``True`` (default) sends file responses with
  the ``http.response.pathsend`` extension when the server offers it.

pathsend: a ``FileResponse`` over a regular file on disk is sent as a path
and the server writes the file itself (granian does it in Rust) instead of
Django reading it in a worker thread. Only the plain case qualifies: status
200, not HEAD, no ``Content-Encoding`` (GZipMiddleware rewrites the
stream), and a ``Content-Length`` equal to the file's size with the file at
offset 0 (rules out ranges and slices). Everything else is sent as before.
"""

import asyncio
import contextlib
import contextvars
import os
import weakref

import django
from django.conf import settings
from django.core.handlers.asgi import (
    ASGIHandler,
    ASGIRequest,
)
from django.db.utils import load_backend
from django.http import FileResponse
from django.utils.module_loading import import_string

__all__ = ["AsyncASGIApplication", "AsyncASGIHandler", "get_asgi_application"]

PATHSEND = "http.response.pathsend"

_current_scope = contextvars.ContextVar("django_async_backend_asgi_scope")


def get_asgi_application(*, lifespan=None, websockets=None, databases=None):
    """Set up Django and return the ASGI application.

    ``lifespan``: async context manager factory ``f(state)`` or its dotted
    path (default: ``settings.ASYNC_BACKEND_LIFESPAN``). ``websockets``: a
    list of ``django.urls.path()`` entries mapping to consumer classes (see
    ``django_async_backend.contrib.websockets``), or any ASGI app for
    websocket scopes. ``databases``: overrides
    ``settings.ASYNC_BACKEND_LIFESPAN_DATABASES``.
    """
    django.setup(set_prefix=False)
    return AsyncASGIApplication(
        lifespan=lifespan, websockets=websockets, databases=databases
    )


class StateASGIRequest(ASGIRequest):
    @property
    def state(self):
        """The worker's lifespan state (a per-request shallow copy)."""
        return self.scope.setdefault("state", {})


class AsyncASGIHandler(ASGIHandler):
    """Django's ASGI handler with ``request.state`` and pathsend."""

    request_class = StateASGIRequest

    async def handle(self, scope, receive, send):
        token = _current_scope.set(scope)
        try:
            await super().handle(scope, receive, send)
        finally:
            _current_scope.reset(token)

    async def send_response(self, response, send):
        path = _pathsend_path(response, _current_scope.get(None))
        if path is None:
            return await super().send_response(response, send)
        await send(
            {
                "type": "http.response.start",
                "status": response.status_code,
                "headers": _response_headers(response),
            }
        )
        # Django's handle() closes the response (and the file) afterwards.
        await send({"type": PATHSEND, "path": path})


def _response_headers(response):
    # As ASGIHandler.send_response() builds them.
    headers = []
    for header, value in response.items():
        if isinstance(header, str):
            header = header.encode("ascii")
        if isinstance(value, str):
            value = value.encode("latin1")
        headers.append((bytes(header), bytes(value)))
    for cookie in response.cookies.values():
        headers.append((b"Set-Cookie", cookie.OutputString().encode("ascii")))
    return headers


def _pathsend_path(response, scope):
    """The absolute path to hand to the server, or None to stream as usual."""
    if scope is None or not getattr(settings, "ASYNC_BACKEND_PATHSEND", True):
        return None
    if PATHSEND not in (scope.get("extensions") or {}):
        return None
    if not isinstance(response, FileResponse) or response.status_code != 200:
        return None
    if scope.get("method") == "HEAD" or response.has_header(
        "Content-Encoding"
    ):
        return None
    filelike = getattr(response, "file_to_stream", None)
    name = getattr(filelike, "name", None)
    if not isinstance(name, str) or not os.path.isabs(name):
        return None
    try:
        size = os.fstat(filelike.fileno()).st_size
        at_start = filelike.tell() == 0
    except (AttributeError, OSError, ValueError):
        return None
    if not at_start or response.get("Content-Length") != str(size):
        return None
    return name


class _WorkerState:
    """Lifespan bookkeeping for one event loop (one worker)."""

    def __init__(self):
        self.state = {}
        self.started = False
        # The server drives startup/shutdown itself; never start lazily.
        self.uses_lifespan = False
        self.lock = asyncio.Lock()
        self.stack = None


class AsyncASGIApplication:
    def __init__(self, *, lifespan=None, websockets=None, databases=None):
        self.http = AsyncASGIHandler()
        self._lifespan = lifespan
        self._databases = databases
        if isinstance(websockets, (list, tuple)):
            from django_async_backend.contrib.websockets import WebsocketRouter

            websockets = WebsocketRouter(websockets)
        self.websockets = websockets
        # Granian's free-threaded build runs each worker as a thread with its
        # own loop in one process, so everything is kept per loop.
        self._workers = weakref.WeakKeyDictionary()

    def _worker(self):
        loop = asyncio.get_running_loop()
        worker = self._workers.get(loop)
        if worker is None:
            worker = self._workers.setdefault(loop, _WorkerState())
        return worker

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind == "lifespan":
            return await self._run_lifespan(scope, receive, send)

        worker = self._worker()
        if not worker.started and not worker.uses_lifespan:
            # The server sends no lifespan (e.g. granian's asginl).
            await self._startup(worker)
        if "state" not in scope:
            scope["state"] = worker.state.copy()

        if kind == "http":
            return await self.http(scope, receive, send)
        if kind == "websocket":
            if self.websockets is None:
                await receive()  # websocket.connect
                # Closing before accepting refuses the handshake (HTTP 403).
                await send({"type": "websocket.close", "code": 4404})
                return
            return await self.websockets(scope, receive, send)
        raise ValueError("Unsupported ASGI scope type: %s." % kind)

    # ---- lifespan -----------------------------------------------------

    async def _run_lifespan(self, scope, receive, send):
        worker = self._worker()
        worker.uses_lifespan = True
        if isinstance(scope.get("state"), dict):
            # Share the server's dict: it copies it into each request scope.
            worker.state = scope["state"]
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    await self._startup(worker)
                except Exception as exc:
                    await send(
                        {
                            "type": "lifespan.startup.failed",
                            "message": "%s: %s" % (type(exc).__name__, exc),
                        }
                    )
                    raise
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                try:
                    await self._shutdown(worker)
                except Exception as exc:
                    await send(
                        {
                            "type": "lifespan.shutdown.failed",
                            "message": "%s: %s" % (type(exc).__name__, exc),
                        }
                    )
                    raise
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _startup(self, worker):
        async with worker.lock:
            if worker.started:
                return
            stack = contextlib.AsyncExitStack()
            try:
                stack.push_async_callback(_close_pools)
                await _open_databases(self._database_mode())
                hook = self._lifespan_hook()
                if hook is not None:
                    await stack.enter_async_context(hook(worker.state))
            except BaseException:
                await stack.aclose()
                raise
            worker.stack = stack
            worker.started = True

    async def _shutdown(self, worker):
        async with worker.lock:
            stack, worker.stack = worker.stack, None
            worker.started = False
            if stack is not None:
                await stack.aclose()

    def _lifespan_hook(self):
        hook = self._lifespan
        if hook is None:
            hook = getattr(settings, "ASYNC_BACKEND_LIFESPAN", None)
        if isinstance(hook, str):
            hook = import_string(hook)
        return hook

    def _database_mode(self):
        if self._databases is not None:
            return self._databases
        return getattr(settings, "ASYNC_BACKEND_LIFESPAN_DATABASES", "pools")


def _async_aliases():
    from django_async_backend.db.utils import async_connections

    for alias, settings_dict in async_connections.settings.items():
        try:
            backend = load_backend(settings_dict["ENGINE"])
        except ImportError:
            continue
        if hasattr(backend, "AsyncDatabaseWrapper"):
            yield alias, backend


def _wrapper(alias, backend):
    from django_async_backend.db.utils import async_connections

    # Built directly, not via async_connections[...]: this task doesn't own
    # it, and it never joins the orphan reaper's watch list.
    return backend.AsyncDatabaseWrapper(
        async_connections.settings[alias], alias
    )


async def _open_databases(mode):
    if not mode:
        return
    for alias, backend in _async_aliases():
        wrapper = _wrapper(alias, backend)
        pool = getattr(wrapper, "pool", None)
        if pool is not None:
            await pool.open(wait=True)
        elif mode is True:
            await wrapper.ensure_connection()
            await wrapper.close()


async def _close_pools():
    for alias, backend in _async_aliases():
        pools_for_loop = getattr(
            backend.AsyncDatabaseWrapper, "_pools_for_current_loop", None
        )
        if pools_for_loop is None:
            continue
        pool = pools_for_loop().pop(alias, None)
        if pool is not None:
            await pool.close()
