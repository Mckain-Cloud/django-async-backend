"""Changelist and autocomplete views on the async ORM."""

import functools
import inspect

from django.contrib.admin.views.autocomplete import AutocompleteJsonView
from django.contrib.admin.views.main import (
    ChangeList,
    IncorrectLookupParameters,
)
from django.core.exceptions import PermissionDenied
from django.core.paginator import (
    InvalidPage,
    Paginator,
)
from django.http import JsonResponse

from django_async_backend.contrib.admin.utils import PreparedPaginator
from django_async_backend.db.models.convert import to_async_queryset


class _DateHierarchyQuerySet:
    """Stand-in for ``cl.queryset`` once results are loaded.

    The ``date_hierarchy`` template tag calls ``aggregate(Min, Max)`` and
    ``dates()``/``datetimes()`` on it while rendering. Those answers are
    computed here ahead of time; everything else is delegated.
    """

    def __init__(self, queryset, date_range, buckets):
        self._queryset = queryset
        self._date_range = date_range
        self._buckets = buckets

    @classmethod
    async def abuild(cls, cl):
        from django.contrib.admin.utils import get_fields_from_path
        from django.db import models as dj_models

        field_name = cl.date_hierarchy
        field = get_fields_from_path(cl.model, field_name)[-1]
        kind_method = (
            "datetimes"
            if isinstance(field, dj_models.DateTimeField)
            else "dates"
        )
        year = cl.params.get("%s__year" % field_name)
        month = cl.params.get("%s__month" % field_name)
        day = cl.params.get("%s__day" % field_name)
        date_range = None
        if not (year or month or day):
            date_range = await cl.queryset.aaggregate(
                first=dj_models.Min(field_name), last=dj_models.Max(field_name)
            )
            # The tag narrows to year/month when the range is within one.
            first, last = date_range["first"], date_range["last"]
            if first and last and first.year == last.year:
                year = first.year
                if first.month == last.month:
                    month = first.month
        if year and month and day:
            buckets = {}
        elif year and month:
            kind = "day"
            buckets = {
                kind: [
                    d
                    async for d in getattr(cl.queryset, kind_method)(
                        field_name, kind
                    )
                ]
            }
        elif year:
            kind = "month"
            buckets = {
                kind: [
                    d
                    async for d in getattr(cl.queryset, kind_method)(
                        field_name, kind
                    )
                ]
            }
        else:
            kind = "year"
            buckets = {
                kind: [
                    d
                    async for d in getattr(cl.queryset, kind_method)(
                        field_name, kind
                    )
                ]
            }
        return cls(cl.queryset, date_range, buckets)

    def aggregate(self, *args, **kwargs):
        if self._date_range is None:
            raise TypeError("date range was not precomputed for these params")
        return self._date_range

    def _prepared(self, field_name, kind, *args, **kwargs):
        try:
            return self._buckets[kind]
        except KeyError:
            raise TypeError(
                "date buckets for %r were not precomputed" % kind
            ) from None

    def dates(self, field_name, kind, *args, **kwargs):
        return self._prepared(field_name, kind)

    def datetimes(self, field_name, kind, *args, **kwargs):
        return self._prepared(field_name, kind)

    def __getattr__(self, name):
        return getattr(self._queryset, name)

    def __getitem__(self, k):
        return self._queryset[k]

    def __aiter__(self):
        return self._queryset.__aiter__()


class AsyncChangeList(ChangeList):
    """``ChangeList`` whose results are fetched by ``await aget_results()``.

    Django's constructor evaluates the page synchronously; here it only
    builds the queryset, and the view awaits the evaluation.
    """

    def get_results(self, request):
        # Deferred to aget_results().
        self.result_count = None
        self.full_result_count = None
        self.result_list = []
        self.can_show_all = True
        self.multi_page = False
        self.paginator = None

    async def aget_results(self, request):
        # A get_queryset() override may hand back a sync queryset: run it
        # natively rather than through Django's thread-backed __aiter__.
        self.root_queryset = to_async_queryset(self.root_queryset)
        self.queryset = to_async_queryset(self.queryset)
        # Filters that defer their choices load them now; drop the ones that
        # turn out to have nothing to show.
        for spec in self.filter_specs:
            if hasattr(spec, "aload"):
                await spec.aload()
        self.filter_specs = [
            spec for spec in self.filter_specs if spec.has_output()
        ]
        self.has_filters = bool(self.filter_specs)
        if self.add_facets:
            for spec in self.filter_specs:
                if hasattr(spec, "aload_facets"):
                    await spec.aload_facets(self)

        if self.date_hierarchy:
            self.queryset = await _DateHierarchyQuerySet.abuild(self)

        result_count = await self.queryset.acount()
        if self.model_admin.show_full_result_count:
            full_result_count = await self.root_queryset.acount()
        else:
            full_result_count = None
        can_show_all = result_count <= self.list_max_show_all
        multi_page = result_count > self.list_per_page

        # A sync Paginator with a known count serves the templates (they
        # read count/num_pages and call get_elided_page_range()).
        paginator = PreparedPaginator.build(
            Paginator, self.queryset, self.list_per_page, result_count
        )
        if (self.show_all and can_show_all) or not multi_page:
            result_list = [obj async for obj in self.queryset._clone()]
        else:
            try:
                page = paginator.page(
                    self.page_num
                )  # count is cached: no query
            except InvalidPage:
                raise IncorrectLookupParameters
            result_list = [obj async for obj in page.object_list]

        self.result_count = result_count
        self.show_full_result_count = self.model_admin.show_full_result_count
        self.show_admin_actions = not self.show_full_result_count or bool(
            full_result_count
        )
        self.full_result_count = full_result_count
        self.result_list = result_list
        self.can_show_all = can_show_all
        self.multi_page = multi_page
        self.paginator = paginator
        await self._aprepare_list_display()
        return self

    async def _aprepare_list_display(self):
        """Evaluate ``async def`` list_display entries ahead of rendering.

        ``lookup_field()`` calls list_display callables synchronously while
        the result rows render. An async callable (on the ModelAdmin, the
        model, or passed directly) is awaited here for every row and the
        entry is replaced by a sync stand-in that returns the stored value.
        """
        prepared = []
        replaced = {}
        for entry in self.list_display:
            func = None
            kind = None
            if callable(entry):
                func, kind = entry, "callable"
            elif isinstance(entry, str):
                admin_attr = getattr(self.model_admin, entry, None)
                model_attr = getattr(self.model, entry, None)
                if entry != "__str__" and admin_attr is not None:
                    func, kind = admin_attr, "admin"
                elif model_attr is not None:
                    func, kind = model_attr, "model"
            if func is None or not inspect.iscoroutinefunction(func):
                prepared.append(entry)
                continue
            values = {}
            for obj in self.result_list:
                if kind == "model":
                    values[obj.pk] = await getattr(obj, entry)()
                else:
                    values[obj.pk] = await func(obj)

            def stand_in(obj, _values=values):
                return _values.get(obj.pk)

            functools.update_wrapper(stand_in, func)
            if isinstance(entry, str):
                stand_in.__name__ = entry
                if not hasattr(stand_in, "short_description"):
                    stand_in.short_description = entry.replace("_", " ")
            prepared.append(stand_in)
            replaced[entry] = stand_in
        if replaced:
            self.list_display = prepared
            # Links are matched by identity with list_display entries.
            self.list_display_links = [
                replaced.get(link, link) for link in self.list_display_links
            ]


class AsyncAutocompleteJsonView(AutocompleteJsonView):
    async def get(self, request, *args, **kwargs):
        (
            self.term,
            self.model_admin,
            self.source_field,
            to_field_name,
        ) = self.process_request(request)
        if not self.has_perm(request):
            raise PermissionDenied
        queryset = self.get_queryset()
        count = await queryset.acount()
        paginator = PreparedPaginator.build(
            Paginator, queryset, self.paginate_by, count
        )
        try:
            page_number = int(request.GET.get("page", 1))
        except ValueError:
            page_number = 1
        try:
            page = paginator.page(page_number)
        except InvalidPage:
            page = paginator.page(1)
        objects = [obj async for obj in page.object_list]
        return JsonResponse(
            {
                "results": [
                    self.serialize_result(obj, to_field_name)
                    for obj in objects
                ],
                "pagination": {"more": page.has_next()},
            }
        )
