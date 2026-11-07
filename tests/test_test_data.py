"""The class-level transaction, fixtures, asetUpTestData() and the
assertion helpers of AsyncioTestCase / AsyncioTransactionTestCase."""

import asyncio

from test_app.models import (
    M2MOwnerModel,
    TestModel,
)

from django_async_backend.db import async_connections
from django_async_backend.test import (
    AsyncioTestCase,
    AsyncioTransactionTestCase,
    release_task_ownership,
)


class SetUpTestDataTests(AsyncioTestCase):
    fixtures = ["test_fixture"]

    @classmethod
    async def asetUpTestData(cls):
        cls.extra = await TestModel.async_objects.acreate(
            name="class extra", value=99
        )
        cls.fixture_child = await TestModel.async_objects.aget(pk=2)
        cls.owner = await M2MOwnerModel.async_objects.aget(
            name="fixture owner"
        )

    async def test_fixture_rows_and_class_data_visible(self):
        names = [
            n
            async for n in TestModel.async_objects.order_by("pk").values_list(
                "name", flat=True
            )
        ]
        self.assertEqual(
            names, ["fixture parent", "fixture child", "class extra"]
        )
        self.assertEqual(self.fixture_child.relative_id, 1)
        tags = sorted(
            [
                t
                async for t in self.owner.tags.async_objects.values_list(
                    "name", flat=True
                )
            ]
        )
        self.assertEqual(tags, ["fixture tag a", "fixture tag b"])

    async def test_sequences_reset_after_fixture(self):
        # Fixture pks 1 and 2, class data pk 3: the sequence must be past
        # the explicit fixture pks.
        self.assertEqual(self.extra.pk, 3)
        obj = await TestModel.async_objects.acreate(name="later", value=1)
        self.assertGreater(obj.pk, 3)

    async def test_per_test_savepoint_a(self):
        self.assertEqual(await TestModel.async_objects.acount(), 3)
        await TestModel.async_objects.acreate(name="only in a", value=1)
        self.assertEqual(await TestModel.async_objects.acount(), 4)

    async def test_per_test_savepoint_b(self):
        self.assertEqual(await TestModel.async_objects.acount(), 3)
        await TestModel.async_objects.acreate(name="only in b", value=1)
        self.assertEqual(await TestModel.async_objects.acount(), 4)

    async def test_connection_is_the_class_connection(self):
        connection = async_connections["default"]
        self.assertTrue(connection.in_atomic_block)
        # Class transaction plus this test's savepoint.
        self.assertEqual(len(connection.savepoint_ids), 1)
        self.assertIs(connection._task, asyncio.current_task())

    async def test_class_data_is_deep_copied_per_test(self):
        self.extra.name = "mutated"
        self.assertEqual(type(self).extra.name, "class extra")
        self.assertIsNot(self.extra, type(self).extra)

    async def test_assert_num_queries_context(self):
        async with self.assertNumQueries(1):
            await TestModel.async_objects.acount()
        with self.assertRaises(AssertionError):
            async with self.assertNumQueries(2):
                await TestModel.async_objects.acount()

    async def test_assert_num_queries_callable(self):
        await self.assertNumQueries(1, TestModel.async_objects.acount)
        await self.assertNumQueries(
            1, TestModel.async_objects.filter(value__gt=1).acount
        )

    async def test_assert_queryset_equal(self):
        qs = TestModel.async_objects.order_by("pk").values_list(
            "name", flat=True
        )
        await self.assertQuerySetEqual(
            qs, ["fixture parent", "fixture child", "class extra"]
        )
        await self.assertQuerySetEqual(
            TestModel.async_objects.all(),
            ["class extra", "fixture child", "fixture parent"],
            transform=lambda o: o.name,
            ordered=False,
        )
        with self.assertRaises(ValueError):
            await self.assertQuerySetEqual(
                TestModel.async_objects.all(), ["a", "b"]
            )
        with self.assertRaises(AssertionError):
            await self.assertQuerySetEqual(qs, ["fixture parent"])

    async def test_capture_on_commit_callbacks(self):
        calls = []

        async def async_hook():
            calls.append("async")

        def sync_hook():
            calls.append("sync")

        connection = async_connections["default"]
        async with self.captureOnCommitCallbacks(execute=True) as callbacks:
            await connection.on_commit(async_hook)
            await connection.on_commit(sync_hook)
        self.assertEqual(len(callbacks), 2)
        self.assertEqual(calls, ["async", "sync"])

        async with self.captureOnCommitCallbacks() as callbacks:
            await connection.on_commit(async_hook)
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(calls, ["async", "sync"])


class DefaultSetUpTestDataTests(AsyncioTestCase):
    """No fixtures, no asetUpTestData override."""

    async def test_starts_empty(self):
        self.assertEqual(await TestModel.async_objects.acount(), 0)
        await TestModel.async_objects.acreate(name="x", value=1)

    async def test_still_empty(self):
        self.assertEqual(await TestModel.async_objects.acount(), 0)


class OnlyDefaultDatabaseTests(AsyncioTestCase):
    databases = {"default"}

    async def test_only_default_in_transaction(self):
        self.assertEqual(list(self.atomic_cms), ["default"])
        self.assertTrue(async_connections["default"].in_atomic_block)


class TransactionTestCaseFixtureTests(AsyncioTransactionTestCase):
    fixtures = ["test_fixture"]

    async def test_fixtures_loaded_a(self):
        self.assertEqual(await TestModel.async_objects.acount(), 2)
        await TestModel.async_objects.acreate(name="extra a", value=1)
        self.assertEqual(await TestModel.async_objects.acount(), 3)

    async def test_fixtures_loaded_b(self):
        # The previous test's rows were flushed and the fixture reloaded.
        self.assertEqual(await TestModel.async_objects.acount(), 2)
        self.assertFalse(async_connections["default"].in_atomic_block)


class ReleaseTaskOwnershipTests(AsyncioTestCase):
    async def test_next_task_claims_the_connection(self):
        connection = async_connections["default"]
        self.assertIs(connection._task, asyncio.current_task())
        release_task_ownership()
        self.assertIsNone(connection._task)

        async def other_task():
            await TestModel.async_objects.acount()
            return connection._task

        owner = await asyncio.create_task(other_task())
        self.assertIsNot(owner, asyncio.current_task())
        # And this task may not use it any more.
        with self.assertRaises(RuntimeError):
            await TestModel.async_objects.acount()
        connection._task = asyncio.current_task()
