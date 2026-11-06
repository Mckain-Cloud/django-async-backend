"""URLconf for the async admin tests."""

from django.contrib.admin import (
    ShowFacets,
    register,
)
from django.urls import path
from test_app.models import (
    DatesModel,
    GenericChildModel,
    GenericRelationModel,
    M2MOwnerModel,
    M2MTagModel,
    SelectRelatedAuthorModel,
    SelectRelatedBookModel,
    SelectRelatedPublisherModel,
    TestModel,
)

from django_async_backend.contrib.admin import (
    AsyncAdminSite,
    AsyncGenericTabularInline,
    AsyncModelAdmin,
    AsyncSimpleListFilter,
    AsyncTabularInline,
)

site = AsyncAdminSite(name="admin")


class NameInitialFilter(AsyncSimpleListFilter):
    title = "initial"
    parameter_name = "initial"

    async def alookups(self, request, model_admin):
        names = [
            n
            async for n in DatesModel.async_objects.values_list(
                "name", flat=True
            ).order_by("name")
        ]
        return sorted({(n[0], n[0].upper()) for n in names if n})

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(name__startswith=self.value())
        return queryset


@register(DatesModel, site=site)
class DatesAdmin(AsyncModelAdmin):
    list_display = ["name", "date"]
    list_filter = [NameInitialFilter, "date"]
    date_hierarchy = "date"
    show_facets = ShowFacets.ALWAYS


class GenericChildInline(AsyncGenericTabularInline):
    model = GenericChildModel
    fields = ["name"]
    extra = 1


@register(GenericRelationModel, site=site)
class GenericRelationAdmin(AsyncModelAdmin):
    inlines = [GenericChildInline]


class BookInline(AsyncTabularInline):
    model = SelectRelatedBookModel
    fields = ["title", "publisher"]
    extra = 1


@register(SelectRelatedAuthorModel, site=site)
class AuthorAdmin(AsyncModelAdmin):
    list_display = ["name"]
    search_fields = ["name"]
    inlines = [BookInline]


@register(SelectRelatedBookModel, site=site)
class BookAdmin(AsyncModelAdmin):
    list_display = ["title", "author", "publisher"]
    list_filter = ["author", "publisher", "title"]
    search_fields = ["title"]
    readonly_fields = ["publisher"]
    list_per_page = 5


@register(SelectRelatedPublisherModel, site=site)
class PublisherAdmin(AsyncModelAdmin):
    search_fields = ["name"]


@register(M2MOwnerModel, site=site)
class OwnerAdmin(AsyncModelAdmin):
    filter_horizontal = ["tags"]


site.register(M2MTagModel)


@register(TestModel, site=site)
class TestModelAdmin(AsyncModelAdmin):
    list_display = ["name", "value", "relative"]
    list_editable = ["value"]
    raw_id_fields = ["relative"]


urlpatterns = [path("admin/", site.urls)]
