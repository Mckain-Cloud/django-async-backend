"""List filters whose choices are loaded on the async connection.

Django's ``RelatedFieldListFilter`` and ``AllValuesFieldListFilter`` evaluate
a queryset in their constructor. The variants below defer that to
``await filter.aload()``, which ``AsyncChangeList.aget_results()`` calls.
``async_list_filter()`` maps the entries of ``list_filter`` onto them.
"""

import operator

from django.contrib.admin.filters import (
    AllValuesFieldListFilter,
    BooleanFieldListFilter,
    ChoicesFieldListFilter,
    DateFieldListFilter,
    FieldListFilter,
    RelatedFieldListFilter,
    RelatedOnlyFieldListFilter,
)
from django.contrib.admin.utils import (
    get_fields_from_path,
    get_last_value_from_parameters,
    get_model_from_relation,
    reverse_field_path,
)
from django.db import models


class _DeferredChoices:
    _loaded = False

    def has_output(self):
        # Unknown until loaded; AsyncChangeList re-checks after aload().
        return True if not self._loaded else super().has_output()


class AsyncRelatedFieldListFilter(_DeferredChoices, RelatedFieldListFilter):
    def __init__(self, field, request, params, model, model_admin, field_path):
        self._request = request
        self._model_admin = model_admin
        super().__init__(
            field, request, params, model, model_admin, field_path
        )

    def field_choices(self, field, request, model_admin):
        return []

    def _limit_choices_to(self):
        return self.field.get_limit_choices_to()

    async def aload(self):
        field = self.field
        rel_model = get_model_from_relation(field)
        ordering = self.field_admin_ordering(
            field, self._request, self._model_admin
        )
        choice_func = operator.attrgetter(
            field.remote_field.get_related_field().attname
            if hasattr(field.remote_field, "get_related_field")
            else "pk"
        )
        qs = rel_model.async_objects.complex_filter(self._limit_choices_to())
        if ordering:
            qs = qs.order_by(*ordering)
        self.lookup_choices = [(choice_func(x), str(x)) async for x in qs]
        self._loaded = True
        return self


class AsyncRelatedOnlyFieldListFilter(AsyncRelatedFieldListFilter):
    def _limit_choices_to(self):
        pk_qs = (
            self._model_admin.get_queryset(self._request)
            .distinct()
            .values_list("%s__pk" % self.field_path, flat=True)
        )
        return {"pk__in": pk_qs}


class AsyncAllValuesFieldListFilter(
    _DeferredChoices, AllValuesFieldListFilter
):
    def __init__(self, field, request, params, model, model_admin, field_path):
        self.lookup_kwarg = field_path
        self.lookup_kwarg_isnull = "%s__isnull" % field_path
        self.lookup_val = params.get(self.lookup_kwarg)
        self.lookup_val_isnull = get_last_value_from_parameters(
            params, self.lookup_kwarg_isnull
        )
        self.empty_value_display = model_admin.get_empty_value_display()
        parent_model, reverse_path = reverse_field_path(model, field_path)
        if model == parent_model:
            queryset = model_admin.get_queryset(request)
        else:
            queryset = parent_model.async_objects.all()
        self._choices_qs = (
            queryset.distinct()
            .order_by(field.name)
            .values_list(field.name, flat=True)
        )
        self.lookup_choices = []
        FieldListFilter.__init__(
            self, field, request, params, model, model_admin, field_path
        )

    async def aload(self):
        self.lookup_choices = [v async for v in self._choices_qs]
        self._loaded = True
        return self


_ASYNC_FOR = {
    RelatedOnlyFieldListFilter: AsyncRelatedOnlyFieldListFilter,
    RelatedFieldListFilter: AsyncRelatedFieldListFilter,
    AllValuesFieldListFilter: AsyncAllValuesFieldListFilter,
}


def async_field_list_filter_class(field):
    """What ``FieldListFilter.create()`` would pick, made async if needed."""
    if field.remote_field:
        return AsyncRelatedFieldListFilter
    if isinstance(field, models.BooleanField):
        return BooleanFieldListFilter
    if field.choices:
        return ChoicesFieldListFilter
    if isinstance(field, models.DateField):
        return DateFieldListFilter
    return AsyncAllValuesFieldListFilter


def async_list_filter(entry, model):
    """Map one ``list_filter`` entry to its async-safe form."""
    if isinstance(entry, (tuple, list)):
        field_path, cls = entry
        return (field_path, _ASYNC_FOR.get(cls, cls))
    if isinstance(entry, str):
        field = get_fields_from_path(model, entry)[-1]
        return (entry, async_field_list_filter_class(field))
    # A SimpleListFilter subclass: its lookups() must not query.
    return entry
