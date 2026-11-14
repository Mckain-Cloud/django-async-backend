import asyncio
import warnings

from django.conf import settings
from django.contrib.auth import (
    BACKEND_SESSION_KEY,
    HASH_SESSION_KEY,
    SESSION_KEY,
)
from django.contrib.auth.models import User
from django.urls import path
from test_app.models import TestModel

from django_async_backend.contrib.sessions import SessionStore
from django_async_backend.contrib.websockets import (
    WebsocketCommunicator,
    WebsocketConsumer,
    WebsocketRouter,
)
from django_async_backend.db.orphans import UnclosedConnectionWarning
from django_async_backend.test import AsyncioTransactionTestCase

ORIGIN = (b"origin", b"http://testserver")


class EchoConsumer(WebsocketConsumer):
    async def connect(self):
        await self.send_json({"type": "hello", "user": self.user.username})

    async def receive_json(self, data):
        if data.get("type") == "fail":
            raise ValueError("boom")
        count = await TestModel.async_objects.acount()  # ORM inside a handler
        await self.send_json({"type": "echo", "data": data, "rows": count})


class AnonymousConsumer(WebsocketConsumer):
    login_required = False

    async def connect(self):
        await self.send_json(
            {"type": "hello", "authenticated": self.user.is_authenticated}
        )


class StaffConsumer(EchoConsumer):
    async def has_permission(self, user):
        return user.is_staff


class RouteConsumer(AnonymousConsumer):
    async def connect(self):
        await self.send_json(self.scope["url_route"]["kwargs"])


class SpawnConsumer(AnonymousConsumer):
    cancelled = []

    async def connect(self):
        self.spawn(self._forever())

    async def _forever(self):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            SpawnConsumer.cancelled.append(True)
            raise


class PartnerConsumer(EchoConsumer):
    allowed_origins = [".partner.example.com", "testserver"]


class DisconnectConsumer(AnonymousConsumer):
    codes = []

    async def disconnect(self, code):
        DisconnectConsumer.codes.append(code)


router = WebsocketRouter(
    [
        path("ws/echo", EchoConsumer),
        path("ws/anon", AnonymousConsumer),
        path("ws/staff", StaffConsumer),
        path("ws/item/<int:pk>", RouteConsumer),
        path("ws/spawn", SpawnConsumer),
        path("ws/disconnect", DisconnectConsumer),
        path("ws/partners", PartnerConsumer),
    ]
)


class WebsocketTests(AsyncioTransactionTestCase):
    async def login(self, username="alice", is_staff=False):
        user = User(username=username, is_staff=is_staff)
        user.set_password("pw")
        await user.async_save()
        session = SessionStore()
        await session.aset(SESSION_KEY, str(user.pk))
        await session.aset(
            BACKEND_SESSION_KEY, settings.AUTHENTICATION_BACKENDS[0]
        )
        await session.aset(HASH_SESSION_KEY, user.get_session_auth_hash())
        await session.asave()
        cookie = "%s=%s" % (settings.SESSION_COOKIE_NAME, session.session_key)
        return (b"cookie", cookie.encode())

    def communicator(self, path, *headers):
        return WebsocketCommunicator(router, path, headers=list(headers))

    async def test_session_user_echo_and_ping(self):
        cookie = await self.login()
        ws = self.communicator("/ws/echo", cookie, ORIGIN)
        self.assertEqual(await ws.connect(), (True, None))
        self.assertEqual(
            await ws.receive_json_from(), {"type": "hello", "user": "alice"}
        )
        await ws.send_json_to({"type": "ping"})
        self.assertEqual(await ws.receive_json_from(), {"type": "pong"})
        await ws.send_json_to({"type": "hi", "id": 1})
        self.assertEqual(
            await ws.receive_json_from(),
            {"type": "echo", "data": {"type": "hi", "id": 1}, "rows": 0},
        )
        await ws.disconnect()

    async def test_no_session_is_refused(self):
        ws = self.communicator("/ws/echo", ORIGIN)
        self.assertEqual(await ws.connect(), (False, 4403))

    async def test_stale_session_hash_is_refused(self):
        cookie = await self.login()
        user = await User.async_objects.aget(username="alice")
        user.set_password("changed")  # invalidates the session hash
        await user.async_save()
        ws = self.communicator("/ws/echo", cookie, ORIGIN)
        self.assertEqual(await ws.connect(), (False, 4403))

    async def test_foreign_origin_is_refused(self):
        cookie = await self.login()
        for origin in (b"https://evil.example", b"null"):
            with self.subTest(origin=origin):
                ws = self.communicator("/ws/echo", cookie, (b"origin", origin))
                self.assertEqual(await ws.connect(), (False, 4403))

    async def test_wildcard_allowed_hosts_does_not_open_origins(self):
        # Behind an ingress ALLOWED_HOSTS is often "*"; the default check is
        # same-origin and doesn't consult it.
        cookie = await self.login()
        with self.settings(ALLOWED_HOSTS=["*"]):
            ws = self.communicator(
                "/ws/echo", cookie, (b"origin", b"https://evil.example")
            )
            self.assertEqual(await ws.connect(), (False, 4403))

    async def test_origin_must_match_host_header(self):
        cookie = await self.login()
        ws = self.communicator(
            "/ws/echo",
            cookie,
            (b"host", b"app.example.com"),
            (b"origin", b"https://app.example.com"),
        )
        connected, _ = await ws.connect()
        self.assertTrue(connected)
        await ws.disconnect()
        ws = self.communicator(
            "/ws/echo",
            cookie,
            (b"host", b"app.example.com"),
            (b"origin", b"https://other.example.com"),
        )
        self.assertEqual(await ws.connect(), (False, 4403))

    async def test_explicit_allowed_origins(self):
        cookie = await self.login()
        ws = self.communicator(
            "/ws/partners", cookie, (b"origin", b"https://partner.example.com")
        )
        connected, _ = await ws.connect()
        self.assertTrue(connected)
        await ws.disconnect()
        ws = self.communicator(
            "/ws/partners", cookie, (b"origin", b"https://evil.example")
        )
        self.assertEqual(await ws.connect(), (False, 4403))

    async def test_missing_origin_is_allowed(self):
        # Not a browser, so not a cross-site hijack.
        cookie = await self.login()
        ws = self.communicator("/ws/echo", cookie)
        connected, _ = await ws.connect()
        self.assertTrue(connected)
        await ws.disconnect()

    async def test_anonymous_consumer(self):
        ws = self.communicator("/ws/anon", ORIGIN)
        self.assertEqual(await ws.connect(), (True, None))
        self.assertEqual(
            await ws.receive_json_from(),
            {"type": "hello", "authenticated": False},
        )
        await ws.disconnect()

    async def test_has_permission(self):
        cookie = await self.login("bob")
        ws = self.communicator("/ws/staff", cookie, ORIGIN)
        self.assertEqual(await ws.connect(), (False, 4403))
        staff = await self.login("carol", is_staff=True)
        ws = self.communicator("/ws/staff", staff, ORIGIN)
        connected, _ = await ws.connect()
        self.assertTrue(connected)
        await ws.disconnect()

    async def test_unknown_path_and_url_kwargs(self):
        ws = self.communicator("/ws/nope", ORIGIN)
        self.assertEqual(await ws.connect(), (False, 4404))
        ws = self.communicator("/ws/item/42", ORIGIN)
        await ws.connect()
        self.assertEqual(await ws.receive_json_from(), {"pk": 42})
        await ws.disconnect()

    async def test_handler_error_is_reported_and_socket_stays_open(self):
        cookie = await self.login()
        ws = self.communicator("/ws/echo", cookie, ORIGIN)
        await ws.connect()
        await ws.receive_json_from()  # hello
        await ws.send_json_to({"type": "fail", "id": 7})
        self.assertEqual(
            await ws.receive_json_from(),
            {"type": "error", "id": 7, "error": "boom"},
        )
        await ws.send_to(text="not json")
        self.assertEqual(
            await ws.receive_json_from(),
            {"type": "error", "error": "Invalid JSON"},
        )
        await ws.send_json_to({"type": "ping"})
        self.assertEqual(await ws.receive_json_from(), {"type": "pong"})
        await ws.disconnect()

    async def test_spawned_tasks_are_cancelled_on_disconnect(self):
        SpawnConsumer.cancelled.clear()
        ws = self.communicator("/ws/spawn", ORIGIN)
        await ws.connect()
        await asyncio.sleep(0)
        await ws.disconnect()
        self.assertEqual(SpawnConsumer.cancelled, [True])

    async def test_disconnect_hook_gets_the_code(self):
        DisconnectConsumer.codes.clear()
        ws = self.communicator("/ws/disconnect", ORIGIN)
        await ws.connect()
        await ws.disconnect(code=4001)
        self.assertEqual(DisconnectConsumer.codes, [4001])

    async def test_connections_are_closed_between_messages(self):
        # Every handler runs under async_acquire(): nothing is left for the
        # orphan reaper when the socket's task ends.
        cookie = await self.login()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", UnclosedConnectionWarning)
            ws = self.communicator("/ws/echo", cookie, ORIGIN)
            await ws.connect()
            await ws.receive_json_from()
            await ws.send_json_to({"type": "hi"})
            await ws.receive_json_from()
            await ws.disconnect()
            for _ in range(5):
                await asyncio.sleep(0)
        self.assertEqual(
            [w for w in caught if w.category is UnclosedConnectionWarning], []
        )
