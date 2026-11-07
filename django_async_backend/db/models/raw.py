"""``Model.objects.araw(...)``: raw SQL from the sync manager, evaluated on
the async connection.

Django's ``QuerySet.raw()`` returns a sync ``RawQuerySet`` whose ``async for``
is a thread hop. This adds ``araw()`` to Django's ``QuerySet`` and to every
manager (``BaseManager``, so related and custom managers too) returning this
package's async ``RawQuerySet``: ``[row async for row in Model.objects.araw(
"SELECT ...")]`` runs on the request's connection. ``async_objects.araw()``
is the same thing (and ``async_objects.raw()`` always was).
"""

from django.db.models import QuerySet as DjangoQuerySet
from django.db.models.manager import BaseManager

from django_async_backend.db.models.query import DEFAULT_FETCH_MODE
from django_async_backend.db.models.query import RawQuerySet as AsyncRawQuerySet


def araw(self, raw_query, params=(), translations=None, using=None):
    """Mirror of ``QuerySet.raw()`` returning the async ``RawQuerySet``."""
    if using is None:
        using = self.db
    qs = AsyncRawQuerySet(
        raw_query,
        model=self.model,
        params=params,
        translations=translations,
        using=using,
        fetch_mode=getattr(self, "_fetch_mode", DEFAULT_FETCH_MODE),
    )
    qs._prefetch_related_lookups = self._prefetch_related_lookups[:]
    return qs


def _manager_araw(self, raw_query, params=(), translations=None, using=None):
    return self.get_queryset().araw(
        raw_query, params=params, translations=translations, using=using
    )


_manager_araw.__name__ = "araw"


def patch_araw():
    if "araw" not in vars(DjangoQuerySet):
        DjangoQuerySet.araw = araw
    if "araw" not in vars(BaseManager):
        BaseManager.araw = _manager_araw
