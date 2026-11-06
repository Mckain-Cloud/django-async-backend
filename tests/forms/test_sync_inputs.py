from django.core.exceptions import ValidationError
from test_app.models import (
    SelectRelatedAuthorModel,
    SelectRelatedBookModel,
)

from django_async_backend.db.models.query import QuerySet as AsyncQuerySet
from django_async_backend.forms import (
    AsyncModelChoiceField,
    async_modelform_factory,
)
from django_async_backend.test import AsyncioTestCase

BookForm = async_modelform_factory(
    SelectRelatedBookModel, fields=["title", "author"]
)


class AsyncFormSyncInputsTests(AsyncioTestCase):
    async def asyncSetUp(self):
        self.author = await SelectRelatedAuthorModel.async_objects.acreate(
            name="author"
        )

    async def test_sync_queryset_on_async_field_runs_natively(self):
        field = AsyncModelChoiceField(
            queryset=SelectRelatedAuthorModel.objects.all()
        )
        self.assertIsInstance(field.queryset, AsyncQuerySet)
        rows = await field.aload_choices()
        self.assertEqual([r.pk for r in rows], [self.author.pk])
        # Validation runs on the async connection: the row created inside
        # this test's transaction is visible (a thread hop would not see it).
        self.assertEqual(
            (await field.aclean(str(self.author.pk))).pk, self.author.pk
        )

    async def test_model_aclean_is_awaited(self):
        async def aclean(self):
            if await SelectRelatedBookModel.async_objects.filter(
                title=self.title
            ).aexists():
                raise ValidationError({"title": "taken (aclean)"})

        SelectRelatedBookModel.aclean = aclean
        try:
            await SelectRelatedBookModel.async_objects.acreate(
                title="dup", author=self.author
            )
            form = BookForm({"title": "dup", "author": self.author.pk})
            self.assertFalse(await form.ais_valid())
            self.assertIn("taken (aclean)", str(form.errors["title"]))
            form = BookForm({"title": "fresh", "author": self.author.pk})
            self.assertTrue(await form.ais_valid())
        finally:
            del SelectRelatedBookModel.aclean
