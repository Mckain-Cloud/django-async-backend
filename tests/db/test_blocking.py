import asyncio
import inspect
import pickle
import time

import psycopg
from asgiref.sync import sync_to_async
from django.core.exceptions import SynchronousOnlyOperation
from django.db import connections
from django.test import override_settings
from test_app.models import (
    SelectRelatedAuthorModel,
    SelectRelatedProfileModel,
    TestModel,
)

from django_async_backend.db import async_connections
from django_async_backend.db import blocking as db_blocking
from django_async_backend.db.blocking import (
    BlockingQueryError,
    run_blocking,
)
from django_async_backend.db.models.fetch_modes import (
    FETCH_BLOCKING,
    patch_fetch_mode_errors,
)
from django_async_backend.db.transaction import async_atomic
from django_async_backend.test import (
    AsyncioTestCase,
    AsyncioTransactionTestCase,
)


def blocking(queryset):
    return queryset.fetch_mode(FETCH_BLOCKING)


class PsycopgContractTests(AsyncioTestCase):
    """run_blocking() leans on psycopg internals; fail loudly if they move."""

    async def test_private_api_is_where_we_expect_it(self):
        connection = async_connections["default"].connection
        self.assertIsInstance(connection, psycopg.AsyncConnection)
        self.assertTrue(
            inspect.iscoroutinefunction(psycopg.AsyncConnection.wait)
        )
        self.assertEqual(
            list(inspect.signature(psycopg.AsyncConnection.wait).parameters)[
                :2
            ],
            ["self", "gen"],
        )
        self.assertIsNone(connection._pipeline)
        self.assertFalse(connection.lock.locked())
        self.assertIsInstance(connection.pgconn.socket, int)
        self.assertTrue(callable(psycopg.waiting.wait))
        # An instance attribute must be able to shadow the method.
        self.assertTrue(hasattr(connection, "__dict__"))


class FetchBlockingTests(AsyncioTestCase):
    async def asyncSetUp(self):
        db_blocking.reset_reported()
        self.root = await TestModel.async_objects.acreate(name="root")
        self.mid = await TestModel.async_objects.acreate(
            name="mid", relative=self.root
        )
        self.leaf = await TestModel.async_objects.acreate(
            name="leaf", relative=self.mid
        )

    async def test_deferred_field_sees_the_uncommitted_row(self):
        obj = await blocking(TestModel.async_objects.defer("name")).aget(
            pk=self.leaf.pk
        )
        async with self.assertNumQueries(1):
            self.assertEqual(obj.name, "leaf")
            self.assertEqual(obj.name, "leaf")

    async def test_only(self):
        obj = await blocking(TestModel.async_objects.only("name")).aget(
            pk=self.leaf.pk
        )
        self.assertEqual(obj.relative_id, self.mid.pk)

    async def test_forward_relation_chain_inherits_the_mode(self):
        obj = await blocking(TestModel.async_objects).aget(pk=self.leaf.pk)
        async with self.assertNumQueries(2):
            self.assertEqual(obj.relative.relative.name, "root")
        self.assertIs(obj.relative._state.fetch_mode, FETCH_BLOCKING)

    async def test_reverse_one_to_one(self):
        author = await SelectRelatedAuthorModel.async_objects.acreate(
            name="author"
        )
        await SelectRelatedProfileModel.async_objects.acreate(
            author=author, bio="bio"
        )
        obj = await blocking(SelectRelatedAuthorModel.async_objects).aget(
            pk=author.pk
        )
        self.assertEqual(obj.profile.bio, "bio")
        self.assertIs(obj.profile.author, obj)

    async def test_inside_a_server_side_cursor(self):
        names = []
        queryset = blocking(TestModel.async_objects.defer("name"))
        async for obj in queryset.order_by("name").aiterator(chunk_size=1):
            names.append(obj.name)
        self.assertEqual(names, ["leaf", "mid", "root"])

    async def test_the_loop_is_blocked_for_the_query(self):
        ticks = []

        async def ticker():
            while True:
                ticks.append(time.perf_counter())
                await asyncio.sleep(0)

        async def slow():
            connection = async_connections["default"]
            async with await connection.cursor() as cursor:
                await cursor.execute("SELECT pg_sleep(0.2), 7")
                return (await cursor.fetchone())[1]

        task = asyncio.create_task(ticker())
        await asyncio.sleep(0)
        before = len(ticks)
        self.assertEqual(run_blocking(slow(), "default"), 7)
        self.assertEqual(len(ticks), before)
        task.cancel()

    async def test_error_leaves_the_connection_usable(self):
        async def bad():
            connection = async_connections["default"]
            async with await connection.cursor() as cursor:
                await cursor.execute("SELECT 1/0")

        with self.assertRaises(Exception) as cm:
            async with async_atomic():
                run_blocking(bad(), "default")
        self.assertEqual(type(cm.exception).__name__, "DataError")
        self.assertEqual(await TestModel.async_objects.acount(), 3)

    async def test_refused_in_pipeline_mode(self):
        obj = await blocking(TestModel.async_objects.defer("name")).aget(
            pk=self.leaf.pk
        )
        connection = async_connections["default"].connection
        async with connection.pipeline():
            with self.assertRaisesRegex(BlockingQueryError, "pipeline mode"):
                obj.name
        self.assertEqual(obj.name, "leaf")

    async def test_refused_while_the_connection_is_streaming(self):
        obj = await blocking(TestModel.async_objects.defer("name")).aget(
            pk=self.leaf.pk
        )
        connection = async_connections["default"].connection
        rows = []
        cursor = connection.cursor()
        async for row in cursor.stream("SELECT generate_series(1, 3)"):
            with self.assertRaisesRegex(
                BlockingQueryError, "another operation"
            ):
                obj.name
            rows.append(row[0])
        await cursor.close()
        self.assertEqual(rows, [1, 2, 3])
        self.assertEqual(obj.name, "leaf")

    async def test_refused_when_the_coroutine_waits_for_something_else(self):
        async def sleeper():
            await asyncio.sleep(0.01)

        with self.assertRaisesRegex(BlockingQueryError, "tried to wait"):
            run_blocking(sleeper(), "default")
        connection = async_connections["default"].connection
        self.assertNotIn("wait", connection.__dict__)
        self.assertEqual(await TestModel.async_objects.acount(), 3)

    async def test_nested_calls_restore_the_outer_wait(self):
        connection = async_connections["default"].connection

        async def inner():
            return await TestModel.async_objects.acount()

        async def outer():
            first = run_blocking(inner(), "default")
            # Still blocking: this await must not suspend.
            return first + await TestModel.async_objects.acount()

        self.assertEqual(run_blocking(outer(), "default"), 6)
        self.assertNotIn("wait", connection.__dict__)

    async def test_call_site_is_logged_once(self):
        objs = [
            obj
            async for obj in blocking(TestModel.async_objects.defer("name"))
        ]
        with self.assertLogs("django_async_backend.blocking", "WARNING") as cm:
            for obj in objs:
                obj.name
        self.assertEqual(len(cm.output), 1)
        self.assertIn("blocking query on the event loop", cm.output[0])
        self.assertIn("test_blocking.py", cm.output[0])
        self.assertIn("test_call_site_is_logged_once", cm.output[0])

    @override_settings(ASYNC_BACKEND_BLOCKING_QUERIES="off")
    async def test_logging_can_be_turned_off(self):
        obj = await blocking(TestModel.async_objects.defer("name")).aget(
            pk=self.leaf.pk
        )
        with self.assertNoLogs("django_async_backend.blocking", "WARNING"):
            obj.name

    async def test_mode_pickles_to_the_singleton(self):
        self.assertIs(
            pickle.loads(pickle.dumps(FETCH_BLOCKING)), FETCH_BLOCKING
        )
        obj = await blocking(TestModel.async_objects).aget(pk=self.leaf.pk)
        copy = pickle.loads(pickle.dumps(obj))
        self.assertIs(copy._state.fetch_mode, FETCH_BLOCKING)


class DefaultModeErrorTests(AsyncioTestCase):
    async def asyncSetUp(self):
        patch_fetch_mode_errors()
        root = await TestModel.async_objects.acreate(name="root")
        self.leaf = await TestModel.async_objects.acreate(
            name="leaf", relative=root
        )

    async def test_deferred_field_error_names_afetch(self):
        obj = await TestModel.async_objects.defer("name").aget(pk=self.leaf.pk)
        with self.assertRaisesRegex(
            SynchronousOnlyOperation,
            r"TestModel\.name is not loaded.*await obj\.afetch\('name'\)"
            r".*FETCH_BLOCKING",
        ):
            obj.name

    async def test_forward_relation_error_names_afetch(self):
        obj = await TestModel.async_objects.aget(pk=self.leaf.pk)
        with self.assertRaisesRegex(
            SynchronousOnlyOperation, r"await obj\.afetch\('relative'\)"
        ):
            obj.relative
        self.assertEqual((await obj.afetch("relative")).name, "root")
        self.assertEqual(obj.relative.name, "root")


class FetchBlockingThreadTests(AsyncioTransactionTestCase):
    async def asyncTearDown(self):
        await sync_to_async(connections.close_all)()

    async def test_worker_thread_uses_the_sync_path(self):
        root = await TestModel.async_objects.acreate(name="root")
        await TestModel.async_objects.acreate(name="leaf", relative=root)
        obj = await blocking(TestModel.async_objects.defer("name")).aget(
            name="leaf"
        )
        with self.assertNoLogs("django_async_backend.blocking", "WARNING"):
            name = await sync_to_async(lambda: obj.name)()
        self.assertEqual(name, "leaf")

    async def test_no_connection_is_an_error(self):
        root = await TestModel.async_objects.acreate(name="root")
        obj = await blocking(TestModel.async_objects.defer("name")).aget(
            pk=root.pk
        )
        await async_connections["default"].close()
        with self.assertRaisesRegex(BlockingQueryError, "no open async"):
            obj.name
