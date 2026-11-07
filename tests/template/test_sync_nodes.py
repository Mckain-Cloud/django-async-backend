"""Third-party-style tags that query the sync ORM in render()."""

import warnings

from django.core.exceptions import SynchronousOnlyOperation
from django.template import Context
from test_app.models import (
    SelectRelatedAuthorModel,
    SelectRelatedBookModel,
)

from django_async_backend.template import arender_template
from django_async_backend.template import engine as engine_module
from django_async_backend.test import (
    AsyncioTransactionTestCase,
    override_settings,
)
from tests.contrib.test_admin import _close_sync_connections
from tests.template.test_engine import engine


class SyncNodePolicyTests(AsyncioTransactionTestCase):
    async def asyncSetUp(self):
        self.engine = engine()
        self.author = await SelectRelatedAuthorModel.async_objects.acreate(name="al")
        for i in range(2):
            await SelectRelatedBookModel.async_objects.acreate(
                title="b%d" % i, author=self.author
            )
        engine_module._hopped_node_types.clear()

    async def asyncTearDown(self):
        await _close_sync_connections()
        await SelectRelatedBookModel.async_objects.all().adelete()
        await SelectRelatedAuthorModel.async_objects.all().adelete()

    async def render(self):
        template = self.engine.from_string(
            "{% load t %}{% sync_book_count author %}"
        )
        return await arender_template(template, Context({"author": self.author}))

    async def test_default_policy_hops_and_warns_once(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.assertEqual(await self.render(), "2")
            self.assertEqual(await self.render(), "2")
        hops = [w for w in caught if issubclass(w.category, engine_module.AsyncTemplateHopWarning)]
        self.assertEqual(len(hops), 1)
        self.assertIn("sync_book_count", str(hops[0].message))
        self.assertIn("worker thread", str(hops[0].message))

    @override_settings(ASYNC_BACKEND_TEMPLATE_SYNC_NODES="error")
    async def test_error_policy_raises(self):
        with self.assertRaises(SynchronousOnlyOperation):
            await self.render()
