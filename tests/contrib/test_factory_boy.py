import factory
from test_app.models import (
    M2MOwnerModel,
    M2MTagModel,
    SelectRelatedAuthorModel,
    SelectRelatedBookModel,
    SelectRelatedProfileModel,
    SelectRelatedPublisherModel,
)

from django_async_backend.contrib.factory_boy import (
    AsyncDjangoModelFactory,
    AsyncIterator,
    async_post_generation,
)
from django_async_backend.test import AsyncioTestCase


class AuthorFactory(AsyncDjangoModelFactory):
    class Meta:
        model = SelectRelatedAuthorModel
        django_get_or_create = ("name",)

    name = factory.Sequence(lambda n: f"author {n}")


class PublisherFactory(AsyncDjangoModelFactory):
    class Meta:
        model = SelectRelatedPublisherModel

    name = factory.Sequence(lambda n: f"publisher {n}")


class BookFactory(AsyncDjangoModelFactory):
    class Meta:
        model = SelectRelatedBookModel

    title = factory.Sequence(lambda n: f"book {n}")
    author = factory.SubFactory(AuthorFactory)
    publisher = AsyncIterator(
        lambda: SelectRelatedPublisherModel.objects.order_by("name")
    )


class ProfileFactory(AsyncDjangoModelFactory):
    class Meta:
        model = SelectRelatedProfileModel

    author = factory.SubFactory(AuthorFactory)
    bio = "a bio"


class AuthorWithProfileFactory(AuthorFactory):
    profile = factory.RelatedFactory(
        ProfileFactory, factory_related_name="author"
    )


class TagFactory(AsyncDjangoModelFactory):
    class Meta:
        model = M2MTagModel

    name = factory.Sequence(lambda n: f"tag {n}")


HOOK_CALLS = []


class OwnerFactory(AsyncDjangoModelFactory):
    class Meta:
        model = M2MOwnerModel

    name = factory.Sequence(lambda n: f"owner {n}")

    @async_post_generation
    async def tags(self, create, extracted, **kwargs):
        HOOK_CALLS.append((create, self.pk is not None))
        if create and extracted:
            await self.tags.async_objects.aset(extracted)


class FactoryBoyTests(AsyncioTestCase):
    @classmethod
    async def asetUpTestData(cls):
        cls.publishers = await PublisherFactory.acreate_batch(2)
        await BookFactory.aprime(force=True)

    def setUp(self):
        HOOK_CALLS.clear()

    async def test_acreate_saves_subfactory_first(self):
        book = await BookFactory.acreate()
        self.assertIsNotNone(book.pk)
        self.assertIsNotNone(book.author.pk)
        self.assertEqual(book.author_id, book.author.pk)
        fetched = await SelectRelatedBookModel.async_objects.select_related(
            "author"
        ).aget(pk=book.pk)
        self.assertEqual(fetched.author.name, book.author.name)

    async def test_async_iterator_cycles_through_queryset(self):
        await BookFactory.aprime(force=True)  # restart the cycle
        first = await BookFactory.acreate()
        second = await BookFactory.acreate()
        third = await BookFactory.acreate()
        names = sorted(p.name for p in self.publishers)
        self.assertEqual(first.publisher.name, names[0])
        self.assertEqual(second.publisher.name, names[1])
        self.assertEqual(third.publisher.name, names[0])

    async def test_django_get_or_create(self):
        first = await AuthorFactory.acreate(name="shared")
        second = await AuthorFactory.acreate(name="shared")
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            await SelectRelatedAuthorModel.async_objects.filter(
                name="shared"
            ).acount(),
            1,
        )
        # Through a SubFactory as well.
        book = await BookFactory.acreate(author__name="shared")
        self.assertEqual(book.author_id, first.pk)

    async def test_related_factory_is_saved_after_the_instance(self):
        author = await AuthorWithProfileFactory.acreate()
        profile = await SelectRelatedProfileModel.async_objects.aget(
            author=author
        )
        self.assertEqual(profile.bio, "a bio")

    async def test_async_post_generation(self):
        tags = await TagFactory.acreate_batch(2)
        owner = await OwnerFactory.acreate(tags=tags)
        names = sorted(
            [
                n
                async for n in owner.tags.async_objects.values_list(
                    "name", flat=True
                )
            ]
        )
        self.assertEqual(names, sorted(t.name for t in tags))
        # The hook ran once the instance had been saved.
        self.assertEqual(HOOK_CALLS, [(True, True)])

    async def test_abuild_runs_hooks_without_saving(self):
        owner = await OwnerFactory.abuild()
        self.assertIsNone(owner.pk)
        self.assertEqual(HOOK_CALLS, [(False, False)])
        book = await BookFactory.abuild()
        self.assertIsNone(book.pk)
        self.assertIsNone(book.author.pk)
        self.assertEqual(
            await SelectRelatedBookModel.async_objects.acount(), 0
        )

    async def test_acreate_batch(self):
        books = await BookFactory.acreate_batch(3)
        self.assertEqual(len({b.pk for b in books}), 3)
        self.assertEqual(
            await SelectRelatedBookModel.async_objects.acount(), 3
        )

    async def test_sync_build_cannot_run_async_hooks(self):
        with self.assertRaises(RuntimeError):
            OwnerFactory.build()

    async def test_async_iterator_must_be_primed(self):
        class UnprimedFactory(AsyncDjangoModelFactory):
            class Meta:
                model = SelectRelatedBookModel

            title = "t"
            author = factory.SubFactory(AuthorFactory)
            publisher = AsyncIterator(
                SelectRelatedPublisherModel.objects.all()
            )

        with self.assertRaises(RuntimeError):
            UnprimedFactory.build()
        await UnprimedFactory.aprime()
        self.assertIn(UnprimedFactory.build().publisher, self.publishers)
