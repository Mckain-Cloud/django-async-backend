import os

from django.core.exceptions import SynchronousOnlyOperation
from django.template import (
    Context,
    Engine,
    RequestContext,
)
from django.template.response import TemplateResponse
from django.test import AsyncRequestFactory
from test_app.models import (
    SelectRelatedAuthorModel,
    SelectRelatedBookModel,
    SelectRelatedPublisherModel,
)

from django_async_backend.template import (
    arender,
    arender_response,
    arender_template,
    arender_to_string,
)
from django_async_backend.template.patch import is_patched
from django_async_backend.test import AsyncioTestCase

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")


def engine(**kwargs):
    return Engine(
        dirs=[TEMPLATES_DIR],
        libraries={"t": "tests.template.tags"},
        **kwargs,
    )


class AsyncTemplateTests(AsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = engine()
        self.author = await SelectRelatedAuthorModel.async_objects.acreate(
            name="alice"
        )
        self.pub = await SelectRelatedPublisherModel.async_objects.acreate(
            name="pub"
        )
        self.b1 = await SelectRelatedBookModel.async_objects.acreate(
            title="b1", author=self.author, publisher=self.pub
        )
        self.b2 = await SelectRelatedBookModel.async_objects.acreate(
            title="b2", author=self.author
        )
        # Fresh instances: nothing cached.
        self.author = await SelectRelatedAuthorModel.async_objects.aget(
            pk=self.author.pk
        )
        self.b1 = await SelectRelatedBookModel.async_objects.aget(
            pk=self.b1.pk
        )

    async def render(self, source, ctx):
        template = self.engine.from_string(source)
        return await arender_template(template, Context(ctx))

    async def test_resolve_is_patched_on_startup(self):
        self.assertTrue(is_patched())

    async def test_lazy_foreign_key(self):
        src = "{{ book.author.name }}/{{ book.publisher.name }}"
        template = self.engine.from_string(src)
        with self.assertRaises(SynchronousOnlyOperation):
            template.render(Context({"book": self.b1}))
        self.assertEqual(
            await self.render(src, {"book": self.b1}), "alice/pub"
        )
        # Loaded through the cache: the sync render works now.
        self.assertEqual(
            template.render(Context({"book": self.b1})), "alice/pub"
        )

    async def test_null_foreign_key_and_missing_attribute(self):
        b2 = await SelectRelatedBookModel.async_objects.aget(pk=self.b2.pk)
        self.assertEqual(
            await self.render(
                "[{{ book.publisher.name }}][{{ book.nope }}]", {"book": b2}
            ),
            "[][]",
        )

    async def test_related_manager_loop_count_exists(self):
        src = (
            "{% for b in author.books.all %}{{ b.title }},{% endfor %}"
            "{{ author.books.count }}"
            "{% if author.books.exists %}yes{% endif %}"
            "{{ author.books.first.title }}"
        )
        self.assertEqual(
            await self.render(src, {"author": self.author}), "b1,b2,2yesb1"
        )

    async def test_async_queryset_variable(self):
        qs = SelectRelatedBookModel.async_objects.order_by("title")
        src = "{{ qs|length }}:{% for b in qs %}{{ b.title }}{% endfor %}"
        self.assertEqual(await self.render(src, {"qs": qs}), "2:b1b2")

    async def test_async_method_and_coroutine_attribute(self):
        async def asummary(self):
            n = await SelectRelatedBookModel.async_objects.filter(
                author=self
            ).acount()
            return "%s has %d" % (self.name, n)

        SelectRelatedAuthorModel.asummary = asummary
        try:
            self.assertEqual(
                await self.render(
                    "{{ author.asummary }}", {"author": self.author}
                ),
                "alice has 2",
            )
        finally:
            del SelectRelatedAuthorModel.asummary

    async def test_deferred_field(self):
        book = await SelectRelatedBookModel.async_objects.only("id").aget(
            pk=self.b1.pk
        )
        self.assertEqual(
            await self.render("{{ book.title }}", {"book": book}), "b1"
        )

    async def test_if_with_filter_autoescape_spaceless_ifchanged(self):
        src = (
            "{% if book.author.name == 'alice' and book.publisher %}A{% elif book.author %}B{% else %}C{% endif %}"
            "{% with n=book.author.name %}<{{ n|upper }}>{% endwith %}"
            "{% filter upper %}{{ book.author.name }}{% endfilter %}"
            "{% autoescape off %}{{ raw }}{% endautoescape %}"
            "{% spaceless %} <p> {{ book.author.name }} </p> {% endspaceless %}"
            "{% for b in book.author.books.all %}{% ifchanged b.author.name %}{{ b.author.name }}{% endifchanged %}{% endfor %}"
        )
        out = await self.render(src, {"book": self.b1, "raw": "<b>"})
        self.assertEqual(out, "A<ALICE>ALICE<b><p> alice </p>alice")

    async def test_extends_block_super_include(self):
        template = self.engine.get_template("child.html")
        out = await arender_template(
            template, Context({"author": self.author})
        )
        self.assertEqual(
            out, "<h1>base:alice/child</h1>[b1:alice][b2:alice]<inc>b1</inc>"
        )

    async def test_async_simple_and_inclusion_tags_and_filters(self):
        src = (
            "{% load t %}{% book_count author %}|{% shout author.name %}|"
            "{% author_titles author %}{{ titles|join:'+' }}|{% books_of author %}|"
            "{{ author.books.count|double }}"
        )
        self.assertEqual(
            await self.render(src, {"author": self.author}),
            "2|ALICE|b1+b2|<i>b1</i><i>b2</i>|4",
        )

    async def test_fallback_node_with_lazy_expression(self):
        # {% firstof %} has no async renderer: its expressions are resolved
        # ahead of time and Django's own render() is used.
        src = "{% firstof book.nope book.author.name %}|{% cycle book.author.name 'x' %}"
        self.assertEqual(
            await self.render(src, {"book": self.b1}), "alice|alice"
        )

    async def test_string_if_invalid_and_silent_failures(self):
        e = engine(string_if_invalid="INVALID")
        template = e.from_string("{{ missing }}|{{ book.author.nope }}")
        out = await arender_template(template, Context({"book": self.b1}))
        self.assertEqual(out, "INVALID|INVALID")

    async def test_request_context_and_async_context_processor(self):
        async def processor(request):
            return {
                "from_processor": await SelectRelatedBookModel.async_objects.acount()
            }

        e = engine(
            context_processors=["tests.template.test_engine._processor"]
        )
        global _processor
        _processor = processor
        request = AsyncRequestFactory().get("/")
        template = e.from_string("{{ from_processor }}/")
        out = await arender_template(
            template, RequestContext(request, {}, processors=[])
        )
        self.assertEqual(out, "2/")

    async def test_arender_to_string_and_response(self):
        request = AsyncRequestFactory().get("/")
        with self.settings(
            TEMPLATES=[
                {
                    "BACKEND": "django.template.backends.django.DjangoTemplates",
                    "DIRS": [TEMPLATES_DIR],
                    "OPTIONS": {"libraries": {"t": "tests.template.tags"}},
                }
            ]
        ):
            out = await arender_to_string(
                "inc.html", {"first": self.b1}, request
            )
            self.assertEqual(out, "<inc>b1</inc>")
            response = TemplateResponse(
                request, "child.html", {"author": self.author}
            )
            self.assertFalse(response.is_rendered)
            response = await arender_response(response)
            self.assertTrue(response.is_rendered)
            self.assertIn(b"[b1:alice]", response.content)
            # arender() on a backend template object
            from django.template.loader import get_template

            self.assertEqual(
                await arender(get_template("inc.html"), {"first": self.b1}),
                "<inc>b1</inc>",
            )
