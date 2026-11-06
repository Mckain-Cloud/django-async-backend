"""Changelist and autocomplete views on the async ORM."""

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
        # Filters that defer their choices load them now; drop the ones that
        # turn out to have nothing to show.
        for spec in self.filter_specs:
            if hasattr(spec, "aload"):
                await spec.aload()
        self.filter_specs = [
            spec for spec in self.filter_specs if spec.has_output()
        ]
        self.has_filters = bool(self.filter_specs)

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
        return self


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
