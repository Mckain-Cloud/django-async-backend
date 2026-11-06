from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.test import AsyncClient
from test_app.models import (
    SelectRelatedAuthorModel,
    SelectRelatedBookModel,
)

from django_async_backend.test import AsyncioTransactionTestCase
from tests.contrib.test_admin import (
    _close_sync_connections,
    no_sync_queries,
)

User = get_user_model()


class AsyncListDisplayTests(AsyncioTransactionTestCase):
    async def asyncSetUp(self):
        self.client = AsyncClient()
        self.admin = User(username="root", is_staff=True, is_superuser=True)
        self.admin.set_password("pw")
        await self.admin.async_save()
        a = await SelectRelatedAuthorModel.async_objects.acreate(name="a")
        for i in range(3):
            await SelectRelatedBookModel.async_objects.acreate(
                title="t%d" % i, author=a
            )
        r = await self.client.post(
            "/admin/login/", {"username": "root", "password": "pw"}
        )
        assert r.status_code == 302

    async def asyncTearDown(self):
        await _close_sync_connections()
        for model in (
            LogEntry,
            SelectRelatedBookModel,
            SelectRelatedAuthorModel,
            User,
            Session,
        ):
            await model.async_objects.all().adelete()

    async def test_async_list_display_callable(self):
        with no_sync_queries():
            r = await self.client.get(
                "/admin/test_app/selectrelatedbookmodel/"
            )
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("by same author", body.lower())
        self.assertEqual(body.count('class="field-review_count">3<'), 3)
