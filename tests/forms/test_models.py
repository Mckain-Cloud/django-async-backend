from django.core.exceptions import SynchronousOnlyOperation
from django.db import DEFAULT_DB_ALIAS
from test_app.models import (
    M2MOwnerModel,
    M2MTagModel,
    SelectRelatedAuthorModel,
    SelectRelatedBookModel,
    SelectRelatedPublisherModel,
    TestModel,
)

from django_async_backend.db import async_connections
from django_async_backend.db.transaction import async_atomic
from django_async_backend.forms import (
    AsyncModelChoiceField,
    AsyncModelForm,
    AsyncModelMultipleChoiceField,
    aprepare_instance,
    async_inlineformset_factory,
    async_modelform_factory,
    async_modelformset_factory,
)
from django_async_backend.test import (
    AsyncCaptureQueriesContext,
    AsyncioTestCase,
)

BookForm = async_modelform_factory(
    SelectRelatedBookModel, fields=["title", "author", "publisher"]
)
OwnerForm = async_modelform_factory(M2MOwnerModel, fields=["name", "tags"])
TestForm = async_modelform_factory(TestModel, fields=["name", "value"])


class AsyncModelFormTests(AsyncioTestCase):
    async def asyncSetUp(self):
        self.author = await SelectRelatedAuthorModel.async_objects.acreate(
            name="author"
        )
        self.publisher = (
            await SelectRelatedPublisherModel.async_objects.acreate(name="pub")
        )

    async def test_factory_uses_async_fields(self):
        self.assertTrue(issubclass(BookForm, AsyncModelForm))
        form = BookForm()
        self.assertIsInstance(form.fields["author"], AsyncModelChoiceField)
        self.assertIsInstance(
            OwnerForm().fields["tags"], AsyncModelMultipleChoiceField
        )
        self.assertIs(
            form.fields["author"].queryset.model, SelectRelatedAuthorModel
        )

    async def test_choices_must_be_loaded_before_rendering(self):
        form = BookForm()
        with self.assertRaises(TypeError):
            str(form["author"])
        await form.aload_choices()
        html = str(form["author"])
        self.assertIn('value="%s"' % self.author.pk, html)
        self.assertIn(
            "SelectRelatedAuthorModel object (%s)" % self.author.pk, html
        )

    async def test_create_on_the_async_connection(self):
        form = BookForm(
            {"title": "new", "author": self.author.pk, "publisher": ""}
        )
        async with async_atomic():
            self.assertTrue(await form.ais_valid())
            book = await form.asave()
            # Inside the transaction: visible to async, not to sync.
            self.assertEqual(
                await SelectRelatedBookModel.async_objects.filter(
                    pk=book.pk
                ).acount(),
                1,
            )
            self.assertEqual(
                await SelectRelatedBookModel.objects.filter(
                    pk=book.pk
                ).acount(),
                0,
            )
        self.assertEqual(book.author, self.author)
        self.assertIsNone(book.publisher_id)
        # Sync is_valid()/errors read the cached result.
        self.assertTrue(form.is_valid())

    async def test_invalid_choice(self):
        form = BookForm({"title": "x", "author": 999999})
        self.assertFalse(await form.ais_valid())
        self.assertIn("author", form.errors)

    async def test_unique_validation(self):
        await TestModel.async_objects.acreate(name="taken", value=1)
        form = TestForm({"name": "taken", "value": 2})
        self.assertFalse(await form.ais_valid())
        self.assertEqual(list(form.errors), ["name"])

    async def test_edit_existing(self):
        book = await SelectRelatedBookModel.async_objects.acreate(
            title="old", author=self.author
        )
        form = BookForm(
            {
                "title": "renamed",
                "author": self.author.pk,
                "publisher": self.publisher.pk,
            },
            instance=book,
        )
        self.assertTrue(await form.ais_valid())
        await form.asave()
        await book.async_refresh_from_db()
        self.assertEqual(
            (book.title, book.publisher_id), ("renamed", self.publisher.pk)
        )

    async def test_asave_without_commit(self):
        form = BookForm({"title": "later", "author": self.author.pk})
        self.assertTrue(await form.ais_valid())
        book = await form.asave(commit=False)
        self.assertIsNone(book.pk)
        await book.async_save()
        await form.asave_m2m()
        self.assertTrue(
            await SelectRelatedBookModel.async_objects.filter(
                pk=book.pk
            ).aexists()
        )

    async def test_asave_requires_validation(self):
        form = BookForm({"title": "", "author": self.author.pk})
        with self.assertRaises(ValueError):
            await form.asave()

    async def test_aclean_hooks(self):
        class Form(BookForm):
            async def aclean_title(self):
                return self.cleaned_data["title"].upper()

            async def aclean(self):
                data = self.cleaned_data
                data["title"] = data["title"] + "!"
                return data

        form = Form({"title": "shout", "author": self.author.pk})
        self.assertTrue(await form.ais_valid())
        self.assertEqual(form.cleaned_data["title"], "SHOUT!")

    async def test_m2m_save_is_native(self):
        t1 = await M2MTagModel.async_objects.acreate(name="t1")
        t2 = await M2MTagModel.async_objects.acreate(name="t2")
        form = OwnerForm({"name": "owner", "tags": [t1.pk, t2.pk]})
        async with async_atomic():
            self.assertTrue(await form.ais_valid())
            owner = await form.asave()
            names = sorted(
                [
                    n
                    async for n in owner.tags.async_objects.values_list(
                        "name", flat=True
                    )
                ]
            )
            self.assertEqual(names, ["t1", "t2"])
            self.assertEqual(
                await owner.tags.acount(), 0
            )  # thread hop: other tx
        from asgiref.sync import sync_to_async
        from django.db import connections

        await sync_to_async(connections.close_all)()

    async def test_m2m_invalid_pk(self):
        form = OwnerForm({"name": "owner", "tags": [424242]})
        self.assertFalse(await form.ais_valid())
        self.assertIn("tags", form.errors)

    async def test_bound_instance_with_m2m_needs_aprepare_instance(self):
        owner = await M2MOwnerModel.async_objects.acreate(name="owner")
        tag = await M2MTagModel.async_objects.acreate(name="t")
        await owner.tags.async_objects.aadd(tag)
        owner = await M2MOwnerModel.async_objects.aget(pk=owner.pk)
        with self.assertRaises(SynchronousOnlyOperation):
            OwnerForm(instance=owner)
        await aprepare_instance(owner, OwnerForm)
        async with AsyncCaptureQueriesContext(
            async_connections[DEFAULT_DB_ALIAS]
        ) as ctx:
            form = OwnerForm(instance=owner)
        self.assertEqual(len(ctx), 0)
        self.assertEqual(form.initial["tags"], [tag])


class AsyncModelFormSetTests(AsyncioTestCase):
    async def asyncSetUp(self):
        self.author = await SelectRelatedAuthorModel.async_objects.acreate(
            name="author"
        )
        self.b1 = await SelectRelatedBookModel.async_objects.acreate(
            title="b1", author=self.author
        )
        self.b2 = await SelectRelatedBookModel.async_objects.acreate(
            title="b2", author=self.author
        )

    def _data(self, prefix, rows, initial, total=None, extra=None):
        data = {
            f"{prefix}-TOTAL_FORMS": str(
                total if total is not None else len(rows)
            ),
            f"{prefix}-INITIAL_FORMS": str(initial),
            f"{prefix}-MIN_NUM_FORMS": "0",
            f"{prefix}-MAX_NUM_FORMS": "1000",
        }
        for i, row in enumerate(rows):
            for k, v in row.items():
                data[f"{prefix}-{i}-{k}"] = v
        return data

    async def test_modelformset_edit_add_delete(self):
        FormSet = async_modelformset_factory(
            SelectRelatedBookModel,
            fields=["title", "author"],
            extra=1,
            can_delete=True,
        )
        formset = FormSet(
            queryset=SelectRelatedBookModel.async_objects.order_by("pk")
        )
        with self.assertRaises(TypeError):
            formset.forms  # queryset not loaded yet
        await formset.aload()
        self.assertEqual(len(formset.forms), 3)
        self.assertEqual(formset.forms[0].instance, self.b1)
        self.assertIn(
            'value="%s"' % self.author.pk, str(formset.empty_form["author"])
        )

        data = self._data(
            "form",
            [
                {
                    "id": self.b1.pk,
                    "title": "b1-renamed",
                    "author": self.author.pk,
                },
                {
                    "id": self.b2.pk,
                    "title": "b2",
                    "author": self.author.pk,
                    "DELETE": "on",
                },
                {"id": "", "title": "b3", "author": self.author.pk},
            ],
            initial=2,
        )
        formset = FormSet(
            data, queryset=SelectRelatedBookModel.async_objects.order_by("pk")
        )
        await formset.aload()
        self.assertTrue(await formset.ais_valid(), formset.errors)
        saved = await formset.asave()
        titles = sorted(
            [
                t
                async for t in SelectRelatedBookModel.async_objects.values_list(
                    "title", flat=True
                )
            ]
        )
        self.assertEqual(titles, ["b1-renamed", "b3"])
        self.assertEqual([o.title for o in formset.deleted_objects], ["b2"])
        self.assertEqual(sorted(o.title for o in saved), ["b1-renamed", "b3"])

    async def test_modelformset_duplicate_rows_rejected(self):
        FormSet = async_modelformset_factory(
            TestModel, fields=["name", "value"], extra=2
        )
        data = self._data(
            "form",
            [{"name": "dup", "value": 1}, {"name": "dup", "value": 2}],
            initial=0,
        )
        formset = FormSet(data, queryset=TestModel.async_objects.none())
        await formset.aload()
        self.assertFalse(await formset.ais_valid())
        self.assertTrue(formset.non_form_errors())

    async def test_inline_formset(self):
        FormSet = async_inlineformset_factory(
            SelectRelatedAuthorModel,
            SelectRelatedBookModel,
            fields=["title"],
            extra=1,
        )
        formset = FormSet(instance=self.author)
        await formset.aload()
        self.assertEqual(
            [f.instance.title for f in formset.initial_forms], ["b1", "b2"]
        )

        data = self._data(
            "books",
            [
                {"id": self.b1.pk, "title": "b1"},
                {"id": self.b2.pk, "title": "b2-changed"},
                {"id": "", "title": "b3"},
            ],
            initial=2,
        )
        formset = FormSet(data, instance=self.author)
        await formset.aload()
        self.assertTrue(await formset.ais_valid(), formset.errors)
        await formset.asave()
        titles = sorted(
            [
                t
                async for t in self.author.books.async_objects.values_list(
                    "title", flat=True
                )
            ]
        )
        self.assertEqual(titles, ["b1", "b2-changed", "b3"])
        self.assertEqual(formset.new_objects[0].author_id, self.author.pk)
