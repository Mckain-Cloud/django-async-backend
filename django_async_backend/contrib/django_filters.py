"""django-filter on the async connection.

``FilterSet`` validation is a Django form; its ``ModelChoiceFilter`` and
``ModelMultipleChoiceFilter`` validate against the database
(``ModelChoiceField.to_python`` runs a query), and ``filterset.qs``
evaluates nothing itself but is built on a sync queryset. This module gives:

* ``AsyncModelChoiceFilter`` / ``AsyncModelMultipleChoiceFilter`` using the
  async form fields of this package;
* ``AsyncFilterSet``: those filters by default for relations, an async form
  (``ais_valid()``, ``aerrors``) and ``aqs()`` returning an async queryset;
* ``AsyncDjangoFilterBackend`` for the REST framework views in
  ``django_async_backend.contrib.rest_framework`` (``afilter_queryset()``).

Custom ``FilterSet`` subclasses inherit from ``AsyncFilterSet``; declared
relation filters should use the async filter classes.
"""

from django.db.models import QuerySet as DjangoQuerySet
from django_filters import fields as df_fields
from django_filters import filters
from django_filters.rest_framework import backends
from django_filters.rest_framework import filterset as drf_filterset

from django_async_backend.forms import (
    AsyncFormMixin,
    AsyncModelChoiceField,
    AsyncModelMultipleChoiceField,
)
from django_async_backend.db.models.convert import to_async_queryset
from django_async_backend.db.models.query import QuerySet as AsyncQuerySet

__all__ = [
    "AsyncDjangoFilterBackend",
    "AsyncFilterSet",
    "AsyncFilterModelChoiceField",
    "AsyncFilterModelMultipleChoiceField",
    "AsyncModelChoiceFilter",
    "AsyncModelMultipleChoiceFilter",
]


class AsyncFilterModelChoiceField(df_fields.ModelChoiceField, AsyncModelChoiceField):
    """django-filter's ``ModelChoiceField`` (null choice) with async lookups."""

    async def ato_python(self, value):
        if self.null_label is not None and value == self.null_value:
            return value
        return await super().ato_python(value)


class AsyncFilterModelMultipleChoiceField(
    df_fields.ModelMultipleChoiceField, AsyncModelMultipleChoiceField
):
    async def _acheck_values(self, value):
        null = self.null_label is not None and value and self.null_value in value
        if null:
            value = [v for v in value if v != self.null_value]
        result = list(await super()._acheck_values(value))
        result += [self.null_value] if null else []
        return result


class AsyncModelChoiceFilter(filters.ModelChoiceFilter):
    field_class = AsyncFilterModelChoiceField


class AsyncModelMultipleChoiceFilter(filters.ModelMultipleChoiceFilter):
    field_class = AsyncFilterModelMultipleChoiceField


def _async_defaults(defaults):
    swapped = {}
    for model_field, spec in defaults.items():
        spec = dict(spec)
        if spec.get("filter_class") is filters.ModelChoiceFilter:
            spec["filter_class"] = AsyncModelChoiceFilter
        elif spec.get("filter_class") is filters.ModelMultipleChoiceFilter:
            spec["filter_class"] = AsyncModelMultipleChoiceFilter
        swapped[model_field] = spec
    return swapped


class AsyncFilterSet(drf_filterset.FilterSet):
    FILTER_DEFAULTS = _async_defaults(drf_filterset.FilterSet.FILTER_DEFAULTS)

    def get_form_class(self):
        base = super().get_form_class()
        if issubclass(base, AsyncFormMixin):
            return base
        return type(base.__name__, (AsyncFormMixin, base), {})

    async def ais_valid(self):
        return self.is_bound and not await self.form.aerrors()

    def filter_queryset(self, queryset):
        # django-filter's version asserts a django QuerySet comes back; the
        # async QuerySet is a parallel class, so accept both.
        for name, value in self.form.cleaned_data.items():
            queryset = self.filters[name].filter(queryset, value)
            assert isinstance(queryset, (DjangoQuerySet, AsyncQuerySet)), (
                "Expected '%s.%s' to return a QuerySet, but got a %s instead."
                % (type(self).__name__, name, type(queryset).__name__)
            )
        return queryset

    async def aqs(self):
        if not hasattr(self, "_qs"):
            qs = to_async_queryset(self.queryset.all())
            if self.is_bound:
                await self.form.aerrors()  # validate before filtering
                qs = self.filter_queryset(qs)
            self._qs = qs
        return self._qs


class AsyncDjangoFilterBackend(backends.DjangoFilterBackend):
    filterset_base = AsyncFilterSet

    async def afilter_queryset(self, request, queryset, view):
        filterset = self.get_filterset(request, queryset, view)
        if filterset is None:
            return queryset
        avalid = getattr(filterset, "ais_valid", None)
        if avalid is None:
            raise TypeError(
                "%s must inherit from AsyncFilterSet to be used with "
                "AsyncDjangoFilterBackend." % type(filterset).__name__
            )
        if not await avalid() and self.raise_exception:
            raise backends.utils.translate_validation(filterset.errors)
        return await filterset.aqs()
