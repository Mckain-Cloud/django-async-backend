"""Django admin on the async ORM.

::

    from django_async_backend.contrib.admin import AsyncAdminSite, AsyncModelAdmin

    site = AsyncAdminSite()

    @site.register(Book)
    class BookAdmin(AsyncModelAdmin):
        list_display = ["title", "author"]
        list_filter = ["author"]

    urlpatterns = [path("admin/", site.urls)]

Pair it with ``AsyncModelBackend``, ``django_async_backend.contrib.sessions``
and the async session and message middleware so the whole request stays on
the async connection.
"""

from django_async_backend.contrib.admin.actions import adelete_selected
from django_async_backend.contrib.admin.filters import (
    AsyncAllValuesFieldListFilter,
    AsyncBooleanFieldListFilter,
    AsyncChoicesFieldListFilter,
    AsyncDateFieldListFilter,
    AsyncEmptyFieldListFilter,
    AsyncRelatedFieldListFilter,
    AsyncRelatedOnlyFieldListFilter,
    AsyncSimpleListFilter,
)
from django_async_backend.contrib.admin.options import (
    AsyncGenericInlineModelAdmin,
    AsyncGenericStackedInline,
    AsyncGenericTabularInline,
    AsyncInlineModelAdmin,
    AsyncModelAdmin,
    AsyncStackedInline,
    AsyncTabularInline,
)
from django_async_backend.contrib.admin.sites import AsyncAdminSite
from django_async_backend.contrib.admin.views import AsyncChangeList

__all__ = [
    "AsyncAdminSite",
    "AsyncAllValuesFieldListFilter",
    "AsyncBooleanFieldListFilter",
    "AsyncChangeList",
    "AsyncChoicesFieldListFilter",
    "AsyncDateFieldListFilter",
    "AsyncEmptyFieldListFilter",
    "AsyncGenericInlineModelAdmin",
    "AsyncGenericStackedInline",
    "AsyncGenericTabularInline",
    "AsyncInlineModelAdmin",
    "AsyncModelAdmin",
    "AsyncRelatedFieldListFilter",
    "AsyncRelatedOnlyFieldListFilter",
    "AsyncSimpleListFilter",
    "AsyncStackedInline",
    "AsyncTabularInline",
    "adelete_selected",
]
