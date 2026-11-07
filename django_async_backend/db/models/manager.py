from django.db.models.manager import Manager

from django_async_backend.db.models.query import QuerySet


class AsyncManager(Manager.from_queryset(QuerySet)):
    """``Manager`` over the async ``QuerySet``.

    Subclassing Django's ``Manager`` (rather than ``BaseManager``) keeps
    ``isinstance(manager, Manager)`` true for third-party code. Django's own
    proxies (``get()``, ``count()``...) call the queryset's method of the same
    name, which on the async QuerySet raises a ``TypeError`` naming the async
    twin.
    """
