"""
Websocket consumers on the async connection, without Channels.

Route websocket scopes with ``django.urls.path()`` entries and serve them
with ``get_asgi_application(websockets=...)``::

    # asgi.py
    from django.urls import path
    from django_async_backend.asgi import get_asgi_application

    application = get_asgi_application(
        websockets=[path("ws/queue", QueueConsumer)],
    )

    # consumers.py
    class QueueConsumer(WebsocketConsumer):
        async def connect(self):
            await self.send_json({"type": "hello", "user": self.user.username})

        async def receive_json(self, data):
            if data["type"] == "queue":
                result = await queue_files(data["file_ids"])   # ORM is fine
                await self.send_json({"type": "queued", **result})

What the base class does, so each consumer doesn't:

* **Origin check** (on by default). Browsers send ``Origin`` on every
  websocket handshake and attach cookies *and TLS client certificates* to
  cross-site handshakes, so without the check any page the user visits can
  open the socket as them (cross-site websocket hijacking). By default the
  origin must be the socket's own host (same-origin, compared with the
  ``Host`` header); set ``allowed_origins`` to a list of host patterns
  (``ALLOWED_HOSTS`` syntax) for other sites. ``ALLOWED_HOSTS`` itself is
  not used: behind an ingress it is often ``"*"``. A handshake without
  ``Origin`` (not a browser) is allowed.
* **Authentication** (required by default). ``authenticate()`` resolves the
  user from the session cookie with the configured ``SESSION_ENGINE`` and
  Django's own ``aget_user()`` (backend and session-hash checks included).
  Override it for other credentials (client-certificate headers, tokens);
  set ``login_required = False`` for anonymous sockets and override
  ``has_permission(user)`` to narrow access (``is_staff``...). A refused
  handshake is closed before it is accepted, which servers answer with
  HTTP 403.
* **Database connections**. A websocket is not a request, so the request
  middleware never closes its connections. ``authenticate()``,
  ``connect()``, each ``receive_*()`` and ``disconnect()`` run inside
  ``async_acquire()`` for every alias in ``databases``: a connection is
  opened for the call and closed after it, and an idle socket holds none.
  Use ``async with self.acquire():`` in tasks started with ``spawn()``.
* **Framing**. ``receive_json()`` gets parsed JSON (invalid JSON is
  answered with ``{"type": "error"}``); ``{"type": "ping"}`` is answered
  with ``{"type": "pong"}`` before your code sees it (``ping_type = None``
  turns that off). ``send_json()``, ``send_text()``, ``send_bytes()``,
  ``close()``.
* **Background tasks**. ``spawn(coro)`` starts a task (a Redis pub/sub
  listener, a periodic push) that is cancelled and awaited when the socket
  closes.
* **Errors**. An exception in a handler is logged and, by default, sent to
  the client as ``{"type": "error", "id": <message id>, "error": "..."}``;
  override ``handle_error()`` to change that.

Each socket also runs in its own ``ThreadSensitiveContext``, as Django runs
each HTTP request, so ``sync_to_async()`` calls from different sockets don't
queue on one shared thread.

For tests, ``WebsocketCommunicator(application, "/ws/queue", headers=...)``
drives an ASGI application from a test case.
"""

import asyncio
import contextlib
import json
import logging
from importlib import import_module
from types import SimpleNamespace
from urllib.parse import urlsplit

from asgiref.sync import ThreadSensitiveContext
from django.conf import settings
from django.db import DEFAULT_DB_ALIAS
from django.http.cookie import parse_cookie
from django.http.request import validate_host
from django.urls.exceptions import Resolver404

from django_async_backend.db import async_acquire

__all__ = [
    "WebsocketCommunicator",
    "WebsocketConsumer",
    "WebsocketRouter",
    "aget_session_user",
    "origin_allowed",
]

logger = logging.getLogger("django_async_backend.websockets")

CLOSE_NORMAL = 1000
CLOSE_INTERNAL_ERROR = 1011
CLOSE_FORBIDDEN = 4403
CLOSE_NOT_FOUND = 4404


# ---------------------------------------------------------------- helpers


def scope_headers(scope):
    """The scope's headers as a ``{lowercase name: str}`` dict (last wins)."""
    return {
        name.decode("latin-1").lower(): value.decode("latin-1")
        for name, value in scope.get("headers", [])
    }


def origin_allowed(scope, allowed_hosts=None):
    """True unless a browser ``Origin`` header names another site.

    Default (``allowed_hosts=None``): same-origin. The origin's host must
    equal the host of the ``Host`` header the socket was opened with (ports
    aren't compared: behind a TLS-terminating proxy they don't line up).
    ``ALLOWED_HOSTS`` is deliberately not the default: apps behind an
    ingress often set it to ``"*"``, which would allow every origin.
    With a list, the origin's host must match one of its patterns
    (``ALLOWED_HOSTS`` syntax: ``".example.com"``, ``"*"``...).

    A handshake without ``Origin`` is allowed: browsers always send it, so
    its absence means a non-browser client, not a cross-site page.
    """
    headers = scope_headers(scope)
    origin = headers.get("origin")
    if origin is None:
        return True
    origin_host = urlsplit(origin).hostname
    if not origin_host:  # "null" (sandboxed iframes, file://)
        return False
    if allowed_hosts is not None:
        return validate_host(origin_host, allowed_hosts)
    host = headers.get("host")
    if not host:
        return False
    host = urlsplit("//" + host).hostname
    return bool(host) and origin_host.lower() == host.lower()


async def aget_session_user(scope):
    """The user the session cookie in ``scope`` belongs to, or
    ``AnonymousUser``. Uses ``SESSION_ENGINE`` and Django's ``aget_user()``.
    Sets ``scope["session"]``."""
    from django.contrib.auth import aget_user
    from django.contrib.auth.models import AnonymousUser

    cookie = scope_headers(scope).get("cookie")
    session_key = (
        parse_cookie(cookie).get(settings.SESSION_COOKIE_NAME)
        if cookie
        else None
    )
    if not session_key:
        return AnonymousUser()
    store = import_module(settings.SESSION_ENGINE).SessionStore(session_key)
    scope["session"] = store
    # aget_user() reads (and on a stale hash, flushes) request.session only.
    return await aget_user(SimpleNamespace(session=store))


# ---------------------------------------------------------------- router


class WebsocketRouter:
    """ASGI app for websocket scopes: ``django.urls.path()``/``re_path()``
    entries whose view is a ``WebsocketConsumer`` subclass (or any class
    taking ``(scope)`` and called with ``(receive, send)``). URL kwargs are
    in ``scope["url_route"]``; an unmatched path is refused with 4404."""

    def __init__(self, patterns):
        self.patterns = list(patterns)

    def resolve(self, path):
        path = path.lstrip("/")
        for pattern in self.patterns:
            try:
                match = pattern.resolve(path)
            except Resolver404:
                continue
            if match is not None:
                return match
        return None

    async def __call__(self, scope, receive, send):
        match = self.resolve(scope["path"])
        if match is None:
            await receive()  # websocket.connect
            await send({"type": "websocket.close", "code": CLOSE_NOT_FOUND})
            return
        scope["url_route"] = {"args": match.args, "kwargs": match.kwargs}
        consumer = match.func(scope)
        async with ThreadSensitiveContext():
            await consumer(receive, send)


# ---------------------------------------------------------------- consumer


class WebsocketConsumer:
    #: Aliases opened (lazily) and closed around every handler call.
    databases = (DEFAULT_DB_ALIAS,)
    #: Host patterns for the Origin check; None = same-origin (Host header).
    allowed_origins = None
    #: Refuse the handshake unless authenticate() returns a logged-in user.
    login_required = True
    #: Reply to {"type": <ping_type>} with {"type": "pong"}; None disables.
    ping_type = "ping"
    #: Subprotocol to accept with, or None.
    subprotocol = None

    def __init__(self, scope):
        self.scope = scope
        self.user = None
        self._send = None
        self._tasks = set()
        self._closed = False

    # ---- hooks --------------------------------------------------------

    async def authenticate(self):
        """Return the connecting user (or None/AnonymousUser)."""
        return await aget_session_user(self.scope)

    async def has_permission(self, user):
        return True

    async def connect(self):
        """Called after the handshake is accepted."""

    async def receive_json(self, data):
        """A text frame holding JSON (other than a ping)."""

    async def receive_text(self, text):
        """A text frame. The default parses JSON and calls receive_json()."""
        try:
            data = json.loads(text)
        except ValueError:
            await self.send_json({"type": "error", "error": "Invalid JSON"})
            return
        if (
            self.ping_type is not None
            and isinstance(data, dict)
            and data.get("type") == self.ping_type
        ):
            await self.send_json({"type": "pong"})
            return
        await self.receive_json(data)

    async def receive_bytes(self, data):
        """A binary frame. Refused by default."""
        await self.send_json(
            {"type": "error", "error": "Binary frames are not accepted"}
        )

    async def disconnect(self, code):
        """The socket closed (``code`` from the client, or ours)."""

    async def handle_error(self, exc, payload):
        """A handler raised. Logs it and sends an error frame."""
        logger.exception(
            "Websocket handler failed on %s", self.scope.get("path")
        )
        request_id = payload.get("id") if isinstance(payload, dict) else None
        with contextlib.suppress(Exception):
            await self.send_json(
                {"type": "error", "id": request_id, "error": str(exc)[:300]}
            )

    # ---- API ----------------------------------------------------------

    @contextlib.asynccontextmanager
    async def acquire(self):
        """A connection per alias in ``databases`` for this task, closed on
        exit (reused when the task already owns one)."""
        async with contextlib.AsyncExitStack() as stack:
            for alias in self.databases:
                await stack.enter_async_context(async_acquire(alias))
            yield

    def spawn(self, coro):
        """Run ``coro`` as a task cancelled when the socket closes."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    async def send_json(self, data):
        await self.send_text(json.dumps(data, default=str))

    async def send_text(self, text):
        await self._send({"type": "websocket.send", "text": text})

    async def send_bytes(self, data):
        await self._send({"type": "websocket.send", "bytes": data})

    async def close(self, code=CLOSE_NORMAL):
        if not self._closed:
            self._closed = True
            await self._send({"type": "websocket.close", "code": code})

    # ---- protocol -----------------------------------------------------

    async def __call__(self, receive, send):
        self._send = send
        message = await receive()
        if message["type"] != "websocket.connect":
            return
        if not await self._handshake():
            await send({"type": "websocket.close", "code": CLOSE_FORBIDDEN})
            return
        accept = {"type": "websocket.accept"}
        if self.subprotocol is not None:
            accept["subprotocol"] = self.subprotocol
        await send(accept)

        code = CLOSE_NORMAL
        try:
            await self._call(self.connect)
            while not self._closed:
                message = await receive()
                if message["type"] == "websocket.disconnect":
                    code = message.get("code", CLOSE_NORMAL)
                    self._closed = True
                    break
                if message["type"] != "websocket.receive":
                    continue
                if message.get("text") is not None:
                    await self._call(self.receive_text, message["text"])
                elif message.get("bytes") is not None:
                    await self._call(self.receive_bytes, message["bytes"])
        finally:
            await self._cancel_tasks()
            with contextlib.suppress(Exception):
                async with self.acquire():
                    await self.disconnect(code)

    async def _handshake(self):
        if not origin_allowed(self.scope, self.allowed_origins):
            logger.warning(
                "Websocket %s refused: origin %r not allowed",
                self.scope.get("path"),
                scope_headers(self.scope).get("origin"),
            )
            return False
        async with self.acquire():
            user = await self.authenticate()
            self.user = self.scope["user"] = user
            if self.login_required and not getattr(
                user, "is_authenticated", False
            ):
                return False
            return bool(await self.has_permission(user))

    async def _call(self, handler, *args):
        try:
            async with self.acquire():
                await handler(*args)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            payload = None
            if args and isinstance(args[0], str):
                with contextlib.suppress(ValueError):
                    payload = json.loads(args[0])
            await self.handle_error(exc, payload)

    def _task_done(self, task):
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "Websocket background task failed",
                exc_info=task.exception(),
            )

    async def _cancel_tasks(self):
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------- testing


class WebsocketCommunicator:
    """Drive an ASGI application's websocket path from a test::

    communicator = WebsocketCommunicator(application, "/ws/queue",
                                         headers=[(b"cookie", b"...")])
    connected, code = await communicator.connect()
    await communicator.send_json_to({"type": "ping"})
    assert await communicator.receive_json_from() == {"type": "pong"}
    await communicator.disconnect()
    """

    def __init__(self, application, path, headers=None, subprotocols=None):
        self.application = application
        path, _, query = path.partition("?")
        headers = list(headers or [])
        if not any(name.lower() == b"host" for name, _ in headers):
            # Browsers always send Host; the same-origin check compares it.
            headers.insert(0, (b"host", b"testserver"))
        self.scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "headers": headers,
            "subprotocols": list(subprotocols or []),
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
            "scheme": "ws",
        }
        self._input = asyncio.Queue()
        self._output = asyncio.Queue()
        self._task = None

    async def connect(self, timeout=5):
        """Return ``(True, subprotocol)`` or ``(False, close code)``."""
        self._task = asyncio.ensure_future(
            self.application(self.scope, self._input.get, self._output.put)
        )
        await self._input.put({"type": "websocket.connect"})
        message = await self._next(timeout)
        if message["type"] == "websocket.accept":
            return True, message.get("subprotocol")
        await self._finish(timeout)
        return False, message.get("code")

    async def send_json_to(self, data):
        await self.send_to(text=json.dumps(data))

    async def send_to(self, text=None, bytes=None):
        message = {"type": "websocket.receive"}
        if text is not None:
            message["text"] = text
        if bytes is not None:
            message["bytes"] = bytes
        await self._input.put(message)

    async def receive_from(self, timeout=5):
        message = await self._next(timeout)
        if message["type"] != "websocket.send":
            raise AssertionError("Expected a websocket.send, got %r" % message)
        return message.get("text", message.get("bytes"))

    async def receive_json_from(self, timeout=5):
        return json.loads(await self.receive_from(timeout))

    async def receive_output(self, timeout=5):
        """The next raw ASGI message the application sent."""
        return await self._next(timeout)

    async def receive_nothing(self, timeout=0.1):
        """True if the application sends nothing within ``timeout``."""
        try:
            await asyncio.wait_for(self._output.get(), timeout)
        except TimeoutError:
            return True
        return False

    async def disconnect(self, code=CLOSE_NORMAL, timeout=5):
        await self._input.put({"type": "websocket.disconnect", "code": code})
        await self._finish(timeout)

    async def _next(self, timeout):
        getter = asyncio.ensure_future(self._output.get())
        done, _ = await asyncio.wait(
            {getter, self._task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if getter in done:
            return getter.result()
        getter.cancel()
        if self._task in done:
            self._task.result()  # re-raise the application's exception
            raise AssertionError("The application finished without sending")
        raise TimeoutError("No message from the application in %ss" % timeout)

    async def _finish(self, timeout):
        if self._task is not None:
            await asyncio.wait_for(self._task, timeout)
