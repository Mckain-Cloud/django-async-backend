"""Turn a sync Django ``QuerySet`` into the async one, query and all.

Django's ``QuerySet.__aiter__`` is ``sync_to_async`` over the sync cursor: a
sync queryset handed to an ``async for`` runs on a worker thread and the
*sync* connection without a word. The async form fields, formsets,
changelist and template engine pass what they receive through
``to_async_queryset()`` so a ``Model.objects`` queryset from user code runs
natively instead.
"""

from django.db.models import query as django_query

from django_async_backend.db.models import query as async_query
from django_async_backend.db.models import sql as async_sql

_ITERABLES = {
    "ModelIterable": async_query.ModelIterable,
    "ValuesIterable": async_query.ValuesIterable,
    "ValuesListIterable": async_query.ValuesListIterable,
    "NamedValuesListIterable": async_query.NamedValuesListIterable,
    "FlatValuesListIterable": async_query.FlatValuesListIterable,
}

_COPIED = (
    "_fields",
    "_prefetch_related_lookups",
    "_known_related_objects",
    "_fetch_mode",
    "_for_write",
    "_sticky_filter",
    "_defer_next_filter",
    "_deferred_filter",
)


def is_sync_queryset(value):
    return isinstance(value, django_query.QuerySet) and not isinstance(
        value, async_query.QuerySet
    )


def to_async_queryset(qs):
    """Return an async queryset equivalent to the sync ``qs``.

    Anything that is not an unevaluated sync queryset is returned as is: an
    evaluated one already holds its rows, and a ``RawQuerySet`` has no async
    twin for its query.
    """
    if not is_sync_queryset(qs) or qs._result_cache is not None:
        return qs
    iterable = _ITERABLES.get(qs._iterable_class.__name__)
    if iterable is None:
        return qs
    query = qs.query.chain(async_sql.Query)
    new = async_query.QuerySet(
        model=qs.model, query=query, using=qs._db, hints=qs._hints
    )
    new._iterable_class = iterable
    for name in _COPIED:
        if hasattr(qs, name):
            setattr(new, name, getattr(qs, name))
    return new
