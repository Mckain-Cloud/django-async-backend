"""List filters whose queries run on the async connection.

Django's filters query at two points: some evaluate their choices in the
constructor (``RelatedFieldListFilter``, ``AllValuesFieldListFilter``,
``SimpleListFilter.lookups()``), and with facets enabled every filter runs an
``aggregate()`` from ``choices()`` while the template renders. The classes
below defer both to ``await spec.aload()`` / ``await spec.aload_facets(cl)``,
which ``AsyncChangeList.aget_results()`` calls. ``async_list_filter()`` maps
the entries of ``list_filter`` onto them.
"""

import operator

from django.contrib.admin.filters import (
    AllValuesFieldListFilter,
    BooleanFieldListFilter,
    ChoicesFieldListFilter,
    DateFieldListFilter,
    EmptyFieldListFilter,
    FieldListFilter,
    RelatedFieldListFilter,
    RelatedOnlyFieldListFilter,
    SimpleListFilter,
)
from django.contrib.admin.utils import (
    get_fields_from_path,
    get_last_value_from_parameters,
    get_model_from_relation,
    reverse_field_path,
)
from django.db import models


class AsyncFacetsMixin:
    """Facet counts computed ahead of rendering."""

    _facet_counts = None

    async def aload_facets(self, changelist):
        # ChangeList.get_queryset() rebuilds filter_specs as a side effect;
        # keep the loaded specs the changelist already has.
        saved = (
            changelist.filter_specs,
            changelist.has_filters,
            changelist.has_active_filters,
            changelist.clear_all_filters_qs,
        )
        try:
            filtered_qs = changelist.get_queryset(
                self.request, exclude_parameters=self.expected_parameters()
            )
        finally:
            (
                changelist.filter_specs,
                changelist.has_filters,
                changelist.has_active_filters,
                changelist.clear_all_filters_qs,
            ) = saved
        self._facet_counts = await filtered_qs.aaggregate(
            **self.get_facet_counts(changelist.pk_attname, filtered_qs)
        )
        return self._facet_counts

    def get_facet_queryset(self, changelist):
        if self._facet_counts is None:
            raise TypeError(
                "Facet counts for %s were not loaded; AsyncChangeList."
                "aget_results() awaits aload_facets() when facets are on."
                % type(self).__name__
            )
        return self._facet_counts


class _DeferredChoices(AsyncFacetsMixin):
    _loaded = False

    def has_output(self):
        # Unknown until loaded; AsyncChangeList re-checks after aload().
        return True if not self._loaded else super().has_output()


class AsyncSimpleListFilter(AsyncFacetsMixin, SimpleListFilter):
    """``SimpleListFilter`` whose choices come from ``async def alookups()``.

    Override ``alookups(request, model_admin)`` instead of ``lookups()``;
    ``queryset()`` stays a sync method that only builds a queryset.
    """

    _loaded = False

    def lookups(self, request, model_admin):
        return ()

    async def alookups(self, request, model_admin):
        raise NotImplementedError(
            "AsyncSimpleListFilter subclasses must define alookups()."
        )

    def __init__(self, request, params, model, model_admin):
        self._model_admin = model_admin
        super().__init__(request, params, model, model_admin)

    def has_output(self):
        return True if not self._loaded else super().has_output()

    async def aload(self):
        choices = await self.alookups(self.request, self._model_admin)
        self.lookup_choices = list(choices or ())
        self._loaded = True
        return self

    def get_facet_counts(self, pk_attname, filtered_qs):
        # Same as Django's, with the subquery narrowed to the pk: the async
        # QuerySet is not a django Query, so the ``__in`` lookup does not
        # narrow it itself.
        original_value = self.used_parameters.get(self.parameter_name)
        counts = {}
        for i, choice in enumerate(self.lookup_choices):
            self.used_parameters[self.parameter_name] = choice[0]
            lookup_qs = self.queryset(self.request, filtered_qs)
            if lookup_qs is not None:
                counts[f"{i}__c"] = models.Count(
                    pk_attname,
                    filter=models.Q(pk__in=lookup_qs.values("pk")),
                )
        self.used_parameters[self.parameter_name] = original_value
        return counts


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


# Filters that never query for their choices only need the facet mixin.


class AsyncBooleanFieldListFilter(AsyncFacetsMixin, BooleanFieldListFilter):
    pass


class AsyncChoicesFieldListFilter(AsyncFacetsMixin, ChoicesFieldListFilter):
    pass


class AsyncDateFieldListFilter(AsyncFacetsMixin, DateFieldListFilter):
    pass


class AsyncEmptyFieldListFilter(AsyncFacetsMixin, EmptyFieldListFilter):
    pass


_ASYNC_FOR = {
    RelatedOnlyFieldListFilter: AsyncRelatedOnlyFieldListFilter,
    RelatedFieldListFilter: AsyncRelatedFieldListFilter,
    AllValuesFieldListFilter: AsyncAllValuesFieldListFilter,
    BooleanFieldListFilter: AsyncBooleanFieldListFilter,
    ChoicesFieldListFilter: AsyncChoicesFieldListFilter,
    DateFieldListFilter: AsyncDateFieldListFilter,
    EmptyFieldListFilter: AsyncEmptyFieldListFilter,
}


def async_field_list_filter_class(field):
    """What ``FieldListFilter.create()`` would pick, made async."""
    if field.remote_field:
        return AsyncRelatedFieldListFilter
    if isinstance(field, models.BooleanField):
        return AsyncBooleanFieldListFilter
    if field.choices:
        return AsyncChoicesFieldListFilter
    if isinstance(field, models.DateField):
        return AsyncDateFieldListFilter
    return AsyncAllValuesFieldListFilter


def async_list_filter(entry, model):
    """Map one ``list_filter`` entry to its async-safe form."""
    if isinstance(entry, (tuple, list)):
        field_path, cls = entry
        return (field_path, _ASYNC_FOR.get(cls, cls))
    if isinstance(entry, str):
        field = get_fields_from_path(model, entry)[-1]
        return (entry, async_field_list_filter_class(field))
    # A SimpleListFilter subclass. AsyncSimpleListFilter loads its choices
    # with alookups(); a plain one must not query in lookups().
    return entry
