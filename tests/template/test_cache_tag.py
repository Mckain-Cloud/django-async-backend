"""``{% cache %}`` on the async cache backend."""

from django.core.cache import caches
from django.template import Context, Engine
from test_app.models import SelectRelatedAuthorModel

from django_async_backend.template import arender_template
from django_async_backend.test import AsyncioTestCase
from tests.contrib.test_admin import no_sync_queries
from tests.template.test_engine import TEMPLATES_DIR


class CacheTagTests(AsyncioTestCase):
    async def asyncSetUp(self):
        # A bare Engine has no builtin libraries; register Django's cache tag.
        self.engine = Engine(
            dirs=[TEMPLATES_DIR],
            libraries={"t": "tests.template.tags", "cache": "django.templatetags.cache"},
        )
        self.author = await SelectRelatedAuthorModel.async_objects.acreate(name="al")

    async def asyncTearDown(self):
        await caches["default"].aclear()

    async def test_fragment_is_cached(self):
        template = self.engine.from_string(
            "{% load cache %}{% cache 60 frag author.pk %}{{ author.name }}:{{ n }}{% endcache %}"
        )
        with no_sync_queries():
            first = await arender_template(template, Context({"author": self.author, "n": 1}))
            second = await arender_template(template, Context({"author": self.author, "n": 2}))
        self.assertEqual(first, "al:1")
        self.assertEqual(second, "al:1")  # served from the cache

    async def test_vary_on_and_named_cache(self):
        template = self.engine.from_string(
            '{% load cache %}{% cache 60 frag2 key using="default" %}{{ n }}{% endcache %}'
        )
        with no_sync_queries():
            a = await arender_template(template, Context({"key": "a", "n": 1}))
            b = await arender_template(template, Context({"key": "b", "n": 2}))
            a2 = await arender_template(template, Context({"key": "a", "n": 3}))
        self.assertEqual((a, b, a2), ("1", "2", "1"))
