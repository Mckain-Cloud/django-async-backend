"""Async model forms.

Django's ``ModelForm`` touches the database in four places: the relation
fields' ``to_python`` (``queryset.get()``), ``_post_clean`` (unique and
constraint validation), ``save()`` and ``save_m2m()``, and, for a bound
instance with many-to-many fields, ``__init__`` itself (``model_to_dict``
reads the related managers). The classes below keep Django's form machinery
and move those four points onto the async ORM::

    Form = async_modelform_factory(Book, fields=["title", "author", "tags"])

    book = await Book.async_objects.aget(pk=1)
    await aprepare_instance(book, Form)          # prefetch m2m for initial
    form = Form(request.POST, instance=book)
    await form.aload_choices()                   # select widgets need rows
    if await form.ais_valid():
        await form.asave()

The sync ``is_valid()``/``save()`` keep working for sync views, so one form
class can serve both.
"""

from itertools import chain

from django import forms
from django.core.exceptions import ValidationError
from django.db import models
from django.forms.formsets import DELETION_FIELD_NAME
from django.forms.models import (
    BaseInlineFormSet,
    BaseModelFormSet,
    InlineForeignKeyField,
    ModelChoiceField,
    ModelChoiceIterator,
    ModelForm,
    ModelMultipleChoiceField,
    construct_instance,
)
from django.forms.models import fields_for_model as _fields_for_model  # noqa
from django.forms.models import (
    inlineformset_factory,
    modelform_factory,
    modelformset_factory,
)

from django_async_backend.db.models.convert import to_async_queryset

# --------------------------------------------------------------- fields ----


class AsyncModelChoiceIterator(ModelChoiceIterator):
    """Serve choices from rows loaded with ``aload_choices()``.

    Widgets iterate the field's choices synchronously while rendering, so the
    rows have to be fetched ahead of time. They are kept on the field (not on
    the queryset, whose deep copies drop their cache) so copies made by form
    and formset construction keep them.
    """

    def _rows(self):
        rows = getattr(self.field, "_async_choices", None)
        if rows is None:
            raise TypeError(
                "The choices of %s have not been loaded; await "
                "form.aload_choices() (or field.aload_choices()) before "
                "rendering or iterating them." % self.field.__class__.__name__
            )
        return rows

    def __iter__(self):
        if self.field.empty_label is not None:
            yield ("", self.field.empty_label)
        for obj in self._rows():
            yield self.choice(obj)

    def __len__(self):
        return len(self._rows()) + (
            1 if self.field.empty_label is not None else 0
        )

    def __bool__(self):
        return self.field.empty_label is not None or bool(self._rows())


class AsyncModelChoiceField(ModelChoiceField):
    """``ModelChoiceField`` over an async queryset.

    ``aclean()`` validates on the async connection; ``clean()`` still works
    with the sync ORM when the queryset is a sync one.
    """

    iterator = AsyncModelChoiceIterator

    def _get_queryset(self):
        return self._queryset

    def _set_queryset(self, queryset):
        # A sync queryset from user code (e.g. a formfield_for_* override)
        # would be iterated on a worker thread; run it natively instead.
        if queryset is not None:
            queryset = to_async_queryset(queryset).all()
        self._queryset = queryset
        self.widget.choices = self.choices

    queryset = property(_get_queryset, _set_queryset)

    async def aload_choices(self):
        if self.queryset is None:
            self._async_choices = []
        else:
            self._async_choices = [obj async for obj in self.queryset]
        return self._async_choices

    async def ato_python(self, value):
        if value in self.empty_values:
            return None
        self.validate_no_null_characters(value)
        try:
            key = self.to_field_name or "pk"
            if isinstance(value, self.queryset.model):
                value = getattr(value, key)
            value = await self.queryset.aget(**{key: value})
        except (
            ValueError,
            TypeError,
            self.queryset.model.DoesNotExist,
            ValidationError,
        ):
            raise ValidationError(
                self.error_messages["invalid_choice"],
                code="invalid_choice",
                params={"value": value},
            )
        return value

    async def aclean(self, value):
        value = await self.ato_python(value)
        self.validate(value)
        self.run_validators(value)
        return value

    async def _aclean_bound_field(self, bf):
        value = bf.initial if self.disabled else bf.data
        return await self.aclean(value)


class AsyncModelMultipleChoiceField(
    AsyncModelChoiceField, ModelMultipleChoiceField
):
    async def ato_python(self, value):
        if not value:
            return []
        return await self._acheck_values(value)

    async def aclean(self, value):
        value = self.prepare_value(value)
        if self.required and not value:
            raise ValidationError(
                self.error_messages["required"], code="required"
            )
        elif not self.required and not value:
            return []
        if not isinstance(value, (list, tuple)):
            raise ValidationError(
                self.error_messages["invalid_list"], code="invalid_list"
            )
        objs = await self._acheck_values(value)
        self.run_validators(value)
        return objs

    async def _acheck_values(self, value):
        """Like ``_check_values()`` but returns a list of objects."""
        key = self.to_field_name or "pk"
        try:
            value = frozenset(value)
        except TypeError:
            raise ValidationError(
                self.error_messages["invalid_list"], code="invalid_list"
            )
        for pk in value:
            self.validate_no_null_characters(pk)
            try:
                self.queryset.filter(**{key: pk})
            except (ValueError, TypeError, ValidationError):
                raise ValidationError(
                    self.error_messages["invalid_pk_value"],
                    code="invalid_pk_value",
                    params={"pk": pk},
                )
        objs = [
            o async for o in self.queryset.filter(**{"%s__in" % key: value})
        ]
        pks = {str(getattr(o, key)) for o in objs}
        for val in value:
            if str(val) not in pks:
                raise ValidationError(
                    self.error_messages["invalid_choice"],
                    code="invalid_choice",
                    params={"value": val},
                )
        return objs


def async_formfield_callback(db_field, **kwargs):
    """``formfield_callback`` giving relation fields async form fields."""
    if isinstance(db_field, models.ManyToManyField):
        kwargs.setdefault("form_class", AsyncModelMultipleChoiceField)
        kwargs.setdefault(
            "queryset", db_field.remote_field.model.async_objects
        )
    elif isinstance(db_field, models.ForeignKey):
        kwargs.setdefault("form_class", AsyncModelChoiceField)
        kwargs.setdefault(
            "queryset", db_field.remote_field.model.async_objects
        )
    return db_field.formfield(**kwargs)


# ---------------------------------------------------------------- forms ----


async def aprepare_instance(instance, form_class=None):
    """Prefetch what ``ModelForm.__init__`` reads synchronously.

    ``model_to_dict()`` evaluates every many-to-many relation of a saved
    instance to build the form's initial data. Prefetching them first makes
    that a cache read.
    """
    if instance is None or not instance._state.adding and instance.pk is None:
        return instance
    if instance._state.adding:
        return instance
    opts = form_class._meta if form_class is not None else None
    names = []
    for f in instance._meta.many_to_many:
        if opts is not None:
            if opts.fields is not None and f.name not in opts.fields:
                continue
            if opts.exclude is not None and f.name in opts.exclude:
                continue
        names.append(f.name)
    if names:
        await instance.afetch(*names)
    return instance


class AsyncModelForm(ModelForm):
    """``ModelForm`` with ``ais_valid()``, ``asave()`` and friends."""

    async def aload_choices(self):
        for name, field in self.fields.items():
            if not hasattr(field, "aload_choices"):
                continue
            widget = field.widget
            while hasattr(widget, "widget") and not hasattr(
                widget, "needs_selected_rows_only"
            ):
                widget = widget.widget  # RelatedFieldWidgetWrapper
            if getattr(widget, "needs_selected_rows_only", False):
                # Autocomplete / raw-id widgets only label the current
                # value(s): load those rows, not the whole table.
                value = self[name].value()
                if value in (None, "", []):
                    values = []
                elif isinstance(value, (list, tuple)):
                    values = [getattr(v, "pk", v) for v in value]
                else:
                    values = [getattr(value, "pk", value)]
                key = field.to_field_name or "pk"
                rows = []
                if values and field.queryset is not None:
                    try:
                        rows = [
                            o
                            async for o in field.queryset.filter(
                                **{"%s__in" % key: values}
                            )
                        ]
                    except (ValueError, TypeError, ValidationError):
                        rows = []
                field._async_choices = rows
                widget._async_rows = rows
            else:
                await field.aload_choices()

    async def aerrors(self):
        if self._errors is None:
            await self.afull_clean()
        return self._errors

    async def ais_valid(self):
        return self.is_bound and not await self.aerrors()

    async def afull_clean(self):
        self._errors = forms.utils.ErrorDict(renderer=self.renderer)
        if not self.is_bound:
            return
        self.cleaned_data = {}
        if self.empty_permitted and not self.has_changed():
            return
        await self._aclean_fields()
        await self._aclean_form()
        await self._apost_clean()

    async def _aclean_fields(self):
        for name, bf in self._bound_items():
            field = bf.field
            try:
                if hasattr(field, "_aclean_bound_field"):
                    value = await field._aclean_bound_field(bf)
                else:
                    value = field._clean_bound_field(bf)
                self.cleaned_data[name] = value
                cleaner = getattr(self, "aclean_%s" % name, None)
                if cleaner is not None:
                    self.cleaned_data[name] = await cleaner()
                elif hasattr(self, "clean_%s" % name):
                    self.cleaned_data[name] = getattr(
                        self, "clean_%s" % name
                    )()
            except ValidationError as e:
                self.add_error(name, e)

    async def _aclean_form(self):
        try:
            if hasattr(self, "aclean"):
                cleaned_data = await self.aclean()
            else:
                cleaned_data = self.clean()
        except ValidationError as e:
            self.add_error(None, e)
        else:
            if cleaned_data is not None:
                self.cleaned_data = cleaned_data

    async def _apost_clean(self):
        opts = self._meta
        exclude = self._get_validation_exclusions()
        for name, field in self.fields.items():
            if isinstance(field, InlineForeignKeyField):
                exclude.add(name)
        try:
            self.instance = construct_instance(
                self, self.instance, opts.fields, opts.exclude
            )
        except ValidationError as e:
            self._update_errors(e)
        # Field cleaning: no queries, except that ForeignKey.validate()
        # checks the target row exists. The async choice field has done that
        # already, so skip relation fields.
        relation_names = {
            f.name
            for f in self.instance._meta.fields
            if f.is_relation and (f.many_to_one or f.one_to_one)
        }
        errors = {}
        try:
            self.instance.clean_fields(exclude=exclude | relation_names)
        except ValidationError as e:
            errors = e.update_error_dict(errors)
        # Model.aclean() if the model defines it (it may query), else clean().
        try:
            if hasattr(self.instance, "aclean"):
                await self.instance.aclean()
            else:
                self.instance.clean()
        except ValidationError as e:
            errors = e.update_error_dict(errors)
        if errors:
            self._update_errors(ValidationError(errors))
        if self._validate_unique:
            await self.avalidate_unique()
        if self._validate_constraints:
            await self.avalidate_constraints()

    async def avalidate_unique(self):
        exclude = self._get_validation_exclusions()
        try:
            await self.instance.async_validate_unique(exclude=exclude)
        except ValidationError as e:
            self._update_errors(e)

    async def avalidate_constraints(self):
        exclude = self._get_validation_exclusions()
        try:
            await self.instance.async_validate_constraints(exclude=exclude)
        except ValidationError as e:
            self._update_errors(e)

    async def _asave_m2m(self):
        cleaned_data = self.cleaned_data
        exclude = self._meta.exclude
        fields = self._meta.fields
        opts = self.instance._meta
        for f in chain(opts.many_to_many, opts.private_fields):
            if not hasattr(f, "save_form_data"):
                continue
            if fields and f.name not in fields:
                continue
            if exclude and f.name in exclude:
                continue
            if f.name not in cleaned_data:
                continue
            if isinstance(f, models.ManyToManyField):
                manager = getattr(self.instance, f.attname).async_objects
                await manager.aset(cleaned_data[f.name])
            else:
                # Generic relations and custom private fields: Django's
                # sync hook (thread hop) is all there is.
                f.save_form_data(self.instance, cleaned_data[f.name])

    async def asave(self, commit=True):
        if self._errors is None:
            await self.afull_clean()
        if self._errors:
            raise ValueError(
                "The %s could not be %s because the data didn't validate."
                % (
                    self.instance._meta.object_name,
                    "created" if self.instance._state.adding else "changed",
                )
            )
        if commit:
            await self.instance.async_save()
            await self._asave_m2m()
        else:
            self.asave_m2m = self._asave_m2m
        return self.instance

    asave.alters_data = True


def async_modelform_factory(model, form=AsyncModelForm, **kwargs):
    """``modelform_factory()`` producing an ``AsyncModelForm``."""
    kwargs.setdefault("formfield_callback", async_formfield_callback)
    if not issubclass(form, AsyncModelForm):
        form = type(form.__name__, (AsyncModelForm, form), {})
    return modelform_factory(model, form=form, **kwargs)


# ------------------------------------------------------------- formsets ----


class AsyncBaseModelFormSet(BaseModelFormSet):
    """``BaseModelFormSet`` over async querysets.

    ``await formset.aload()`` evaluates the queryset (Django indexes it
    while building forms) and loads every choice field. Then ``ais_valid()``
    and ``asave()`` mirror their sync counterparts.
    """

    def get_queryset(self):
        if not hasattr(self, "_queryset"):
            if self.queryset is not None:
                qs = to_async_queryset(self.queryset)
            else:
                qs = self.model.async_objects.get_queryset()
            if isinstance(qs, list):
                self._queryset = qs
            else:
                if not qs.totally_ordered:
                    current_ordering = (
                        qs.query.order_by or qs.model._meta.ordering or []
                    )
                    qs = qs.order_by(*current_ordering, "pk")
                self._queryset = qs
        return self._queryset

    async def aload(self):
        qs = self.get_queryset()
        if not isinstance(qs, list):
            self._queryset = [obj async for obj in qs]
        # Choices: on the form class's fields (copied into every form built
        # from now on, including empty_form) and on forms already built.
        for field in self.form.base_fields.values():
            if hasattr(field, "aload_choices"):
                await field.aload_choices()
        if "forms" in self.__dict__:
            for form in self.forms:
                await form.aload_choices()
        return self

    def add_fields(self, form, index):
        super().add_fields(form, index)
        pk_name = self._pk_field.name
        pk_field = form.fields.get(pk_name)
        if isinstance(pk_field, ModelChoiceField) and not isinstance(
            pk_field, AsyncModelChoiceField
        ):
            pk = self._pk_field
            if isinstance(pk, (models.ForeignKey, models.OneToOneField)):
                qs = pk.remote_field.model.async_objects.get_queryset()
            else:
                qs = self.model.async_objects.get_queryset()
            qs = qs.using(form.instance._state.db)
            form.fields[pk_name] = AsyncModelChoiceField(
                qs,
                initial=pk_field.initial,
                required=False,
                widget=pk_field.widget,
            )

    async def aerrors(self):
        if self._errors is None:
            await self.afull_clean()
        return self._errors

    async def afull_clean(self):
        # Validate every form on the async connection first; Django's sync
        # full_clean() then only reads the cached per-form errors.
        if self.is_bound and self.management_form.is_valid():
            for form in self.forms:
                await form.aerrors()
        self.full_clean()

    async def ais_valid(self):
        if not self.is_bound:
            return False
        await self.aerrors()
        return self.is_valid()

    async def asave_new(self, form, commit=True):
        return await form.asave(commit=commit)

    async def asave_existing(self, form, obj, commit=True):
        return await form.asave(commit=commit)

    async def adelete_existing(self, obj, commit=True):
        if commit:
            await obj.async_delete()

    async def asave(self, commit=True):
        if not commit:
            self.saved_forms = []

            async def asave_m2m():
                for form in self.saved_forms:
                    await form.asave_m2m()

            self.asave_m2m = asave_m2m
        if self.edit_only:
            return await self.asave_existing_objects(commit)
        return await self.asave_existing_objects(
            commit
        ) + await self.asave_new_objects(commit)

    asave.alters_data = True

    async def asave_existing_objects(self, commit=True):
        self.changed_objects = []
        self.deleted_objects = []
        if not self.initial_forms:
            return []
        saved_instances = []
        forms_to_delete = self.deleted_forms
        for form in self.initial_forms:
            obj = form.instance
            if not obj._is_pk_set() or obj._state.adding:
                continue
            if form in forms_to_delete:
                self.deleted_objects.append(obj)
                await self.adelete_existing(obj, commit=commit)
            elif form.has_changed():
                self.changed_objects.append((obj, form.changed_data))
                saved_instances.append(
                    await self.asave_existing(form, obj, commit=commit)
                )
                if not commit:
                    self.saved_forms.append(form)
        return saved_instances

    async def asave_new_objects(self, commit=True):
        self.new_objects = []
        for form in self.extra_forms:
            if not form.has_changed():
                continue
            if self.can_delete and self._should_delete_form(form):
                continue
            self.new_objects.append(await self.asave_new(form, commit=commit))
            if not commit:
                self.saved_forms.append(form)
        return self.new_objects

    def _should_delete_form(self, form):
        return form.cleaned_data.get(DELETION_FIELD_NAME, False)


class AsyncBaseInlineFormSet(AsyncBaseModelFormSet, BaseInlineFormSet):
    def __init__(
        self,
        data=None,
        files=None,
        instance=None,
        save_as_new=False,
        prefix=None,
        queryset=None,
        **kwargs,
    ):
        if queryset is None:
            queryset = self.model.async_objects
        BaseInlineFormSet.__init__(
            self,
            data,
            files,
            instance=instance,
            save_as_new=save_as_new,
            prefix=prefix,
            queryset=queryset,
            **kwargs,
        )

    async def asave_new(self, form, commit=True):
        setattr(form.instance, self.fk.name, self.instance)
        return await super().asave_new(form, commit=commit)


def async_modelformset_factory(
    model, form=AsyncModelForm, formset=AsyncBaseModelFormSet, **kwargs
):
    kwargs.setdefault("formfield_callback", async_formfield_callback)
    if not issubclass(form, AsyncModelForm):
        form = type(form.__name__, (AsyncModelForm, form), {})
    if not issubclass(formset, AsyncBaseModelFormSet):
        formset = type(formset.__name__, (AsyncBaseModelFormSet, formset), {})
    return modelformset_factory(model, form=form, formset=formset, **kwargs)


def async_inlineformset_factory(
    parent_model,
    model,
    form=AsyncModelForm,
    formset=AsyncBaseInlineFormSet,
    **kwargs,
):
    kwargs.setdefault("formfield_callback", async_formfield_callback)
    if not issubclass(form, AsyncModelForm):
        form = type(form.__name__, (AsyncModelForm, form), {})
    if not issubclass(formset, AsyncBaseInlineFormSet):
        formset = type(formset.__name__, (AsyncBaseInlineFormSet, formset), {})
    return inlineformset_factory(
        parent_model, model, form=form, formset=formset, **kwargs
    )


# ---------------------------------------------------- generic inlines ----


class AsyncBaseGenericInlineFormSet(AsyncBaseModelFormSet):
    """Async ``BaseGenericInlineFormSet``.

    The parent's ContentType must be cached already (``aget_for_model``);
    the admin warms it before building the formset.
    """

    def __init__(
        self,
        data=None,
        files=None,
        instance=None,
        save_as_new=False,
        prefix=None,
        queryset=None,
        **kwargs,
    ):
        from django.contrib.contenttypes.models import ContentType

        opts = self.model._meta
        self.instance = instance
        self.rel_name = (
            opts.app_label
            + "-"
            + opts.model_name
            + "-"
            + self.ct_field.name
            + "-"
            + self.ct_fk_field.name
        )
        self.save_as_new = save_as_new
        if self.instance is None or not self.instance._is_pk_set():
            qs = self.model.async_objects.none()
        else:
            if queryset is None:
                queryset = self.model.async_objects
            qs = queryset.filter(
                **{
                    self.ct_field.name: ContentType.objects.get_for_model(
                        self.instance,
                        for_concrete_model=self.for_concrete_model,
                    ),
                    self.ct_fk_field.name: self.instance.pk,
                }
            )
        super().__init__(
            queryset=qs, data=data, files=files, prefix=prefix, **kwargs
        )

    def initial_form_count(self):
        if self.save_as_new:
            return 0
        return super().initial_form_count()

    @classmethod
    def get_default_prefix(cls):
        opts = cls.model._meta
        return (
            opts.app_label
            + "-"
            + opts.model_name
            + "-"
            + cls.ct_field.name
            + "-"
            + cls.ct_fk_field.name
        )

    async def asave_new(self, form, commit=True):
        from django_async_backend.utils.contenttypes import aget_for_model

        ct = await aget_for_model(
            type(self.instance), for_concrete_model=self.for_concrete_model
        )
        setattr(form.instance, self.ct_field.attname, ct.pk)
        setattr(form.instance, self.ct_fk_field.attname, self.instance.pk)
        return await form.asave(commit=commit)


def async_generic_inlineformset_factory(
    model,
    form=AsyncModelForm,
    formset=AsyncBaseGenericInlineFormSet,
    ct_field="content_type",
    fk_field="object_id",
    fields=None,
    exclude=None,
    extra=3,
    can_order=False,
    can_delete=True,
    max_num=None,
    formfield_callback=None,
    validate_max=False,
    for_concrete_model=True,
    min_num=None,
    validate_min=False,
    absolute_max=None,
    can_delete_extra=True,
):
    from django.contrib.contenttypes.models import ContentType

    opts = model._meta
    ct_field = opts.get_field(ct_field)
    if (
        not isinstance(ct_field, models.ForeignKey)
        or ct_field.remote_field.model != ContentType
    ):
        raise Exception(
            "fk_name '%s' is not a ForeignKey to ContentType" % ct_field
        )
    fk_field = opts.get_field(fk_field)
    exclude = [*(exclude or []), ct_field.name, fk_field.name]
    FormSet = async_modelformset_factory(
        model,
        form=form,
        formfield_callback=formfield_callback,
        formset=formset,
        extra=extra,
        can_delete=can_delete,
        can_order=can_order,
        fields=fields,
        exclude=exclude,
        max_num=max_num,
        validate_max=validate_max,
        min_num=min_num,
        validate_min=validate_min,
        absolute_max=absolute_max,
        can_delete_extra=can_delete_extra,
    )
    FormSet.ct_field = ct_field
    FormSet.ct_fk_field = fk_field
    FormSet.for_concrete_model = for_concrete_model
    return FormSet
