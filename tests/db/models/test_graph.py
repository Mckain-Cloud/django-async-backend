from test_app.models import (
    DeleteModel,
    GenericFkModel,
    SelectRelatedAuthorModel,
    SelectRelatedBookModel,
    TestModel,
)

from django_async_backend.db.models.graph import asave_graph
from django_async_backend.test import AsyncioTestCase
from django_async_backend.utils.contenttypes import aget_for_model


class SaveGraphTests(AsyncioTestCase):
    async def test_saves_dependencies_first(self):
        root = TestModel(name="root", value=1)
        middle = TestModel(name="middle", value=2, relative=root)
        leaf = TestModel(name="leaf", value=3, relative=middle)
        async with self.assertNumQueries(3):
            await asave_graph(leaf)
        self.assertEqual(leaf.relative_id, middle.pk)
        self.assertEqual(middle.relative_id, root.pk)
        self.assertEqual(await TestModel.async_objects.acount(), 3)

    async def test_saved_instances_are_left_alone(self):
        author = await SelectRelatedAuthorModel.async_objects.acreate(
            name="saved"
        )
        book = SelectRelatedBookModel(title="t", author=author)
        async with self.assertNumQueries(1):
            await asave_graph(book)
        self.assertEqual(book.author_id, author.pk)

    async def test_shared_dependency_saved_once(self):
        author = SelectRelatedAuthorModel(name="shared")
        first = SelectRelatedBookModel(title="first", author=author)
        second = SelectRelatedBookModel(title="second", author=author)
        seen = {}
        await asave_graph(first, _seen=seen)
        await asave_graph(second, _seen=seen)
        self.assertEqual(
            await SelectRelatedAuthorModel.async_objects.acount(), 1
        )
        self.assertEqual(first.author_id, second.author_id)

    async def test_generic_foreign_key_target_saved_first(self):
        # Warm the content type cache so the descriptor assignment below
        # does not query.
        await aget_for_model(DeleteModel)
        target = DeleteModel(name="target")
        obj = GenericFkModel(name="generic", content_object=target)
        self.assertIsNone(obj.object_id)
        await asave_graph(obj)
        self.assertIsNotNone(target.pk)
        self.assertEqual(obj.object_id, target.pk)
        fetched = await GenericFkModel.async_objects.aget(pk=obj.pk)
        self.assertEqual(await fetched.afetch("content_object"), target)
