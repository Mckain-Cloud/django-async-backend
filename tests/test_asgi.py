import asyncio
import contextlib
import os
import tempfile

import asgi_urls
from django.test import override_settings

from django_async_backend.asgi import (
    PATHSEND,
    AsyncASGIApplication,
)
from django_async_backend.db.utils import async_connections
from django_async_backend.test import AsyncioTransactionTestCase

CONTENT = b"0123456789" * 1000


class ASGITestCase(AsyncioTransactionTestCase):
    async def asyncSetUp(self):
        self._settings = override_settings(ROOT_URLCONF="asgi_urls")
        self._settings.enable()
        self.addCleanup(self._settings.disable)
        handle = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
        handle.write(CONTENT)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        asgi_urls.FILE["path"] = os.path.abspath(handle.name)

    async def http(
        self, app, path, *, method="GET", extensions=None, state=None
    ):
        """Run one HTTP request through ``app``; return the sent messages."""
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
            "extensions": extensions or {},
        }
        if state is not None:
            scope["state"] = state
        sent = []
        request_sent = False
        never = asyncio.Event()

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {
                    "type": "http.request",
                    "body": b"",
                    "more_body": False,
                }
            await never.wait()  # no disconnect while the view runs

        async def send(message):
            sent.append(message)

        await asyncio.wait_for(app(scope, receive, send), 10)
        return sent

    @staticmethod
    def body(messages):
        return b"".join(
            m.get("body", b"")
            for m in messages
            if m["type"] == "http.response.body"
        )


class PathsendTests(ASGITestCase):
    def app(self):
        return AsyncASGIApplication(databases=False)

    async def test_file_response_is_sent_as_a_path(self):
        messages = await self.http(
            self.app(), "/file", extensions={PATHSEND: {}}
        )
        self.assertEqual(messages[0]["type"], "http.response.start")
        self.assertEqual(messages[0]["status"], 200)
        headers = dict(messages[0]["headers"])
        self.assertEqual(
            headers[b"Content-Length"], str(len(CONTENT)).encode()
        )
        self.assertEqual(
            messages[1:], [{"type": PATHSEND, "path": asgi_urls.FILE["path"]}]
        )

    async def test_without_the_extension_the_file_is_streamed(self):
        messages = await self.http(self.app(), "/file")
        self.assertEqual(self.body(messages), CONTENT)
        self.assertNotIn(PATHSEND, [m["type"] for m in messages])

    async def test_ineligible_responses_are_streamed(self):
        for path, method in (
            ("/file-gzipped", "GET"),  # body rewritten by GZipMiddleware
            ("/file-sliced", "GET"),  # not at offset 0
            ("/text", "GET"),  # not a FileResponse
            ("/file", "HEAD"),
        ):
            with self.subTest(path=path, method=method):
                messages = await self.http(
                    self.app(), path, method=method, extensions={PATHSEND: {}}
                )
                self.assertNotIn(PATHSEND, [m["type"] for m in messages])

    async def test_precompressed_file_is_sent_as_a_path(self):
        # WhiteNoise's .gz/.br: Content-Encoding describes the file itself.
        messages = await self.http(
            self.app(), "/file-precompressed", extensions={PATHSEND: {}}
        )
        self.assertIn((b"Content-Encoding", b"gzip"), messages[0]["headers"])
        self.assertEqual(
            messages[1:], [{"type": PATHSEND, "path": asgi_urls.FILE["path"]}]
        )

    async def test_setting_turns_it_off(self):
        with override_settings(ASYNC_BACKEND_PATHSEND=False):
            messages = await self.http(
                self.app(), "/file", extensions={PATHSEND: {}}
            )
        self.assertEqual(self.body(messages), CONTENT)


class LifespanDriver:
    """Run an app's lifespan scope the way a server does."""

    def __init__(self, app, state=None):
        self.app = app
        self.state = {} if state is None else state
        self.inbox = asyncio.Queue()
        self.outbox = asyncio.Queue()
        self.task = None

    async def startup(self):
        scope = {
            "type": "lifespan",
            "asgi": {"version": "3.0"},
            "state": self.state,
        }
        self.task = asyncio.ensure_future(
            self.app(scope, self.inbox.get, self.outbox.put)
        )
        await self.inbox.put({"type": "lifespan.startup"})
        return await asyncio.wait_for(self.outbox.get(), 10)

    async def shutdown(self):
        await self.inbox.put({"type": "lifespan.shutdown"})
        message = await asyncio.wait_for(self.outbox.get(), 10)
        await asyncio.wait_for(self.task, 10)
        return message


class LifespanTests(ASGITestCase):
    async def test_hook_fills_state_and_requests_see_it(self):
        events = []

        @contextlib.asynccontextmanager
        async def lifespan(state):
            state["value"] = "from startup"
            events.append("startup")
            yield
            events.append("shutdown")

        app = AsyncASGIApplication(lifespan=lifespan, databases=False)
        server = LifespanDriver(app)
        self.assertEqual(
            await server.startup(), {"type": "lifespan.startup.complete"}
        )
        # The server copies its state dict into each request scope.
        messages = await self.http(app, "/state", state=server.state.copy())
        self.assertEqual(self.body(messages), b'{"value": "from startup"}')
        self.assertEqual(
            await server.shutdown(), {"type": "lifespan.shutdown.complete"}
        )
        self.assertEqual(events, ["startup", "shutdown"])

    async def test_hook_by_dotted_path_setting(self):
        with override_settings(
            ASYNC_BACKEND_LIFESPAN="test_asgi.dotted_lifespan",
            ASYNC_BACKEND_LIFESPAN_DATABASES=False,
        ):
            app = AsyncASGIApplication()
            server = LifespanDriver(app)
            await server.startup()
            self.assertEqual(server.state["dotted"], True)
            await server.shutdown()

    async def test_failing_startup_is_reported(self):
        @contextlib.asynccontextmanager
        async def lifespan(state):
            raise ConnectionError("database unreachable")
            yield

        app = AsyncASGIApplication(lifespan=lifespan, databases=False)
        server = LifespanDriver(app)
        message = await server.startup()
        self.assertEqual(message["type"], "lifespan.startup.failed")
        self.assertIn("database unreachable", message["message"])
        with self.assertRaises(ConnectionError):
            await server.task

    async def test_without_lifespan_startup_runs_on_first_request(self):
        calls = []

        @contextlib.asynccontextmanager
        async def lifespan(state):
            calls.append("startup")
            state["value"] = "lazy"
            yield

        app = AsyncASGIApplication(lifespan=lifespan, databases=False)
        first = await self.http(app, "/state")
        second = await self.http(app, "/state")
        self.assertEqual(calls, ["startup"])
        self.assertEqual(self.body(first), b'{"value": "lazy"}')
        self.assertEqual(self.body(second), b'{"value": "lazy"}')

    async def test_no_lazy_restart_after_shutdown(self):
        calls = []

        @contextlib.asynccontextmanager
        async def lifespan(state):
            calls.append("startup")
            yield

        app = AsyncASGIApplication(lifespan=lifespan, databases=False)
        server = LifespanDriver(app)
        await server.startup()
        await server.shutdown()
        await self.http(app, "/text")
        self.assertEqual(calls, ["startup"])

    async def test_pools_open_at_startup_and_close_at_shutdown(self):
        options = async_connections.settings["other"].setdefault("OPTIONS", {})
        options["pool"] = {"min_size": 1, "max_size": 2, "timeout": 5}
        self.addCleanup(options.pop, "pool", None)
        backend_cls = type(async_connections["other"])

        app = AsyncASGIApplication(databases="pools")
        server = LifespanDriver(app)
        self.assertEqual(
            await server.startup(), {"type": "lifespan.startup.complete"}
        )
        pool = backend_cls._pools_for_current_loop()["other"]
        self.assertFalse(pool.closed)
        self.assertGreaterEqual(pool.get_stats()["pool_size"], 1)
        await server.shutdown()
        self.assertTrue(pool.closed)
        self.assertNotIn("other", backend_cls._pools_for_current_loop())

    async def test_websocket_without_router_is_refused(self):
        app = AsyncASGIApplication(databases=False)
        sent = []
        inbox = asyncio.Queue()
        await inbox.put({"type": "websocket.connect"})
        scope = {"type": "websocket", "path": "/ws", "headers": []}
        await app(scope, inbox.get, _appender(sent))
        self.assertEqual(sent, [{"type": "websocket.close", "code": 4404}])


def _appender(target):
    async def send(message):
        target.append(message)

    return send


@contextlib.asynccontextmanager
async def dotted_lifespan(state):
    state["dotted"] = True
    yield
