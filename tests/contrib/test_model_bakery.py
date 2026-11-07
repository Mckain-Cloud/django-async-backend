import datetime

from model_bakery.recipe import (
    Recipe,
    foreign_key,
    related,
    seq,
)
from test_app.models import (
    CascadeChildModel,
    DbDefaultModel,
    DeleteModel,
    GenericFkModel,
    M2MOwnerModel,
    M2MTagModel,
    SelectRelatedAuthorModel,
    SelectRelatedBookModel,
    SelectRelatedProfileModel,
    SelectRelatedPublisherModel,
    UpdateOrCreateModel,
)

from django_async_backend.contrib import model_bakery as abaker
from django_async_backend.test import AsyncioTestCase

author_recipe = Recipe(SelectRelatedAuthorModel, name=seq("recipe author "))
book_recipe = Recipe(
    SelectRelatedBookModel,
    title=seq("recipe book "),
    author=foreign_key(author_recipe),
)
author_with_books = author_recipe.extend(
    books=related(book_recipe, book_recipe)
)


class ModelBakeryTests(AsyncioTestCase):
    async def test_amake_generates_and_saves_foreign_keys(self):
        book = await abaker.amake(SelectRelatedBookModel)
        self.assertIsNotNone(book.pk)
        self.assertIsNotNone(book.author.pk)
        self.assertEqual(book.author_id, book.author.pk)
        self.assertIsNone(book.publisher_id)  # nullable, left alone
        self.assertEqual(
            await SelectRelatedAuthorModel.async_objects.acount(), 1
        )

    async def test_fill_optional(self):
        book = await abaker.amake(SelectRelatedBookModel, _fill_optional=True)
        self.assertIsNotNone(book.publisher_id)
        self.assertEqual(
            await SelectRelatedPublisherModel.async_objects.acount(), 1
        )

    async def test_quantity(self):
        books = await abaker.amake(SelectRelatedBookModel, _quantity=3)
        self.assertEqual(len({b.pk for b in books}), 3)

    async def test_explicit_values_and_prepared_related(self):
        author = abaker.aprepare(SelectRelatedAuthorModel, name="prepared")
        self.assertIsNone(author.pk)
        book = await abaker.amake(
            SelectRelatedBookModel, author=author, title="explicit"
        )
        self.assertEqual(book.title, "explicit")
        self.assertIsNotNone(author.pk)
        self.assertEqual(book.author_id, author.pk)

    async def test_m2m_values(self):
        tags = await abaker.amake(M2MTagModel, _quantity=2)
        owner = await abaker.amake(M2MOwnerModel, tags=tags)
        names = sorted(
            [
                n
                async for n in owner.tags.async_objects.values_list(
                    "name", flat=True
                )
            ]
        )
        self.assertEqual(names, sorted(t.name for t in tags))

    async def test_make_m2m(self):
        owner = await abaker.amake(M2MOwnerModel, make_m2m=True)
        self.assertGreater(await owner.tags.async_objects.acount(), 0)

    async def test_reverse_foreign_key_values(self):
        children = abaker.aprepare(CascadeChildModel, _quantity=2)
        parent = await abaker.amake(DeleteModel, cascade_children=children)
        self.assertEqual(
            await parent.cascade_children.async_objects.acount(), 2
        )
        # The parents prepare() generated for the children were dropped.
        self.assertEqual(await DeleteModel.async_objects.acount(), 1)

    async def test_reverse_one_to_one_value(self):
        profile = abaker.aprepare(SelectRelatedProfileModel, bio="reverse")
        author = await abaker.amake(SelectRelatedAuthorModel, profile=profile)
        saved = await SelectRelatedProfileModel.async_objects.aget(
            author=author
        )
        self.assertEqual(saved.bio, "reverse")
        self.assertEqual(
            await SelectRelatedAuthorModel.async_objects.acount(), 1
        )

    async def test_generic_foreign_key_to_saved_instance(self):
        target = await abaker.amake(DeleteModel)
        obj = await abaker.amake(GenericFkModel, content_object=target)
        self.assertIsNotNone(obj.content_type_id)
        self.assertEqual(obj.object_id, target.pk)
        fetched = await GenericFkModel.async_objects.aget(pk=obj.pk)
        self.assertEqual(await fetched.afetch("content_object"), target)

    async def test_auto_now_override(self):
        when = datetime.datetime(2020, 1, 2, 3, 4, 5)  # USE_TZ is off in tests
        obj = await abaker.amake(UpdateOrCreateModel, updated_at=when)
        self.assertEqual(obj.updated_at, when)
        await obj.async_refresh_from_db()
        self.assertEqual(obj.updated_at, when)

    async def test_refresh_after_create(self):
        obj = await abaker.amake(DbDefaultModel, _refresh_after_create=True)
        self.assertEqual(obj.ts, 7)

    async def test_recipe_with_foreign_key(self):
        first = await abaker.amake_recipe(book_recipe)
        second = await abaker.amake_recipe(book_recipe)
        self.assertTrue(first.title.startswith("recipe book "))
        self.assertNotEqual(first.title, second.title)
        self.assertTrue(first.author.name.startswith("recipe author "))
        self.assertIsNotNone(first.author.pk)

    async def test_recipe_related(self):
        author = await abaker.amake_recipe(author_with_books)
        self.assertEqual(await author.books.async_objects.acount(), 2)
        self.assertEqual(
            await SelectRelatedAuthorModel.async_objects.acount(), 1
        )

    async def test_recipe_by_name_and_quantity(self):
        authors = await abaker.amake_recipe("test_app.author", _quantity=2)
        self.assertEqual(len(authors), 2)
        self.assertTrue(
            all(a.name.startswith("named author ") for a in authors)
        )
        self.assertEqual(
            await SelectRelatedAuthorModel.async_objects.acount(), 2
        )
