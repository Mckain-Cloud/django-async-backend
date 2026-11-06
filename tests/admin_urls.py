"""URLconf for the async admin tests."""

from django.contrib.admin import register
from django.urls import path
from test_app.models import (
    M2MOwnerModel,
    M2MTagModel,
    SelectRelatedAuthorModel,
    SelectRelatedBookModel,
    SelectRelatedPublisherModel,
    TestModel,
)

from django_async_backend.contrib.admin import (
    AsyncAdminSite,
    AsyncModelAdmin,
    AsyncTabularInline,
)

site = AsyncAdminSite(name="admin")


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
