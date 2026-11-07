"""Exercised by tests/test_pytest_plugin.py through a real pytest run."""

import pytest
from test_app.models import TestModel

from django_async_backend.db import async_connections
from django_async_backend.test import AsyncioTestCase


async def test_blocked_without_fixture():
    with pytest.raises(
        RuntimeError, match="Async database access not allowed"
    ):
        await TestModel.async_objects.acount()


async def test_adb_isolation_one(adb):
    assert await TestModel.async_objects.acount() == 0
    await TestModel.async_objects.acreate(name="one", value=1)
    assert await TestModel.async_objects.acount() == 1
    assert async_connections["default"].in_atomic_block


async def test_adb_isolation_two(adb):
    # The previous test's row was rolled back.
    assert await TestModel.async_objects.acount() == 0


@pytest.mark.async_db
async def test_marker_injects_adb():
    await TestModel.async_objects.acreate(name="marked", value=1)
    assert async_connections["default"].in_atomic_block


@pytest.mark.async_db(transaction=True)
async def test_transactional_marker():
    await TestModel.async_objects.acreate(name="committed", value=1)
    assert not async_connections["default"].in_atomic_block
    assert await TestModel.async_objects.acount() == 1


async def test_transactional_flushes(atransactional_db):
    assert await TestModel.async_objects.acount() == 0


async def test_assert_num_queries(
    adb, adjango_assert_num_queries, adjango_assert_max_num_queries
):
    async with adjango_assert_num_queries(1):
        await TestModel.async_objects.acount()
    async with adjango_assert_max_num_queries(5):
        await TestModel.async_objects.acount()
    with pytest.raises(pytest.fail.Exception):
        async with adjango_assert_num_queries(2):
            await TestModel.async_objects.acount()


async def test_admin_client(aadmin_client, aadmin_user):
    assert aadmin_user.is_superuser
    response = await aadmin_client.get("/admin/")
    assert response.status_code == 200


async def test_aclient_anonymous(aclient, adb):
    response = await aclient.get("/admin/login/")
    assert response.status_code == 200


async def test_on_commit(adb, adjango_capture_on_commit_callbacks):
    ran = []

    async def callback():
        ran.append(1)

    async with adjango_capture_on_commit_callbacks(execute=True) as callbacks:
        await async_connections["default"].on_commit(callback)
    assert len(callbacks) == 1
    assert ran == [1]


class UnittestStyle(AsyncioTestCase):
    @classmethod
    async def asetUpTestData(cls):
        cls.obj = await TestModel.async_objects.acreate(
            name="class data", value=5
        )

    async def test_sees_class_data(self):
        self.assertEqual(await TestModel.async_objects.acount(), 1)
        self.assertEqual(self.obj.name, "class data")

    async def test_sees_class_data_again(self):
        self.assertEqual(await TestModel.async_objects.acount(), 1)
