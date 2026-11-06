from django import template as dj_template
from django.template import (
    Context,
    Engine,
)
from test_app.models import (
    SelectRelatedAuthorModel,
    SelectRelatedBookModel,
)

from django_async_backend.template import arender_template
from django_async_backend.test import AsyncioTestCase

register = dj_template.Library()


class CountNode(dj_template.Node):
    def render(self, context):
        raise AssertionError("the sync render must not be used")

    async def arender(self, context):
        return str(await SelectRelatedBookModel.async_objects.acount())


@register.tag
def book_total(parser, token):
    return CountNode()


class NodeProtocolTests(AsyncioTestCase):
    async def test_third_party_node_with_arender(self):
        e = Engine(libraries={"x": "tests.template.test_protocol"})
        t = e.from_string("{% load x %}{% book_total %}")
        self.assertEqual(await arender_template(t, Context({})), "0")

    async def test_sync_queryset_in_template_runs_natively(self):
        author = await SelectRelatedAuthorModel.async_objects.acreate(name="n")
        await SelectRelatedBookModel.async_objects.acreate(
            title="t", author=author
        )
        qs = SelectRelatedBookModel.objects.filter(author=author)  # sync
        t = Engine().from_string(
            "{% for b in qs %}{{ b.title }}{% endfor %}|{{ qs|length }}"
        )
        # Inside this test's transaction: a thread hop would see no rows.
        self.assertEqual(await arender_template(t, Context({"qs": qs})), "t|1")
