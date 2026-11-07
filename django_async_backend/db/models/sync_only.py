"""Loud stand-ins for the sync QuerySet API on the async QuerySet.

The async ``QuerySet`` subclasses Django's so that ``isinstance()`` checks in
Django and third-party code (REST framework, django-filter, forms) take the
queryset branch. Inheritance would also hand back Django's sync methods
(``get()``, ``count()``, ``delete()``...), which would run the async query on
the sync connection: a hidden thread hop at best. Each of those names is
therefore shadowed by a method that raises ``TypeError`` naming the async
twin.
"""


class SyncOnlyError(TypeError):
    pass


def sync_only(name, hint, *, queryset_only=False):
    def method(self, *args, **kwargs):
        raise SyncOnlyError(
            "%s.%s() is synchronous and not available on the async QuerySet; "
            "use `%s`." % (type(self).__name__, name, hint)
        )

    method.__name__ = name
    method.__qualname__ = "QuerySet.%s" % name
    method.__doc__ = "Sync-only on Django's QuerySet; use `%s`." % hint
    if queryset_only:
        method.queryset_only = True
    return method


def _getstate(self):
    # Django's __getstate__ evaluates the queryset first, synchronously.
    import django
    from django.db.models.query import DJANGO_VERSION_PICKLE_KEY

    if self._result_cache is None:
        raise TypeError(
            "Evaluate the async QuerySet before pickling it: "
            "`[obj async for obj in qs]`."
        )
    return {**self.__dict__, DJANGO_VERSION_PICKLE_KEY: django.__version__}


def install_sync_only_stubs(queryset_class):
    """Shadow Django's sync methods on the generated async QuerySet.

    Called at the bottom of the generated module (the codegen's rename rules
    would otherwise rewrite class-body assignments of these names).
    """
    for name, hint in SYNC_ONLY.items():
        if name in vars(queryset_class):
            continue
        setattr(
            queryset_class,
            name,
            sync_only(name, hint, queryset_only=name in QUERYSET_ONLY),
        )
    queryset_class.__getstate__ = _getstate
    queryset_class.__bool__ = _bool


def _bool(self):
    # Inherited from Django, __bool__ would evaluate the queryset synchronously.
    raise TypeError(
        "len() is not supported on %s, and neither is truth-testing. "
        "Use `await qs.aexists()` or `await qs.acount()`." % type(self).__name__
    )


QUERYSET_ONLY = {"delete", "_iterator"}

SYNC_ONLY = {
    "get": "await qs.aget()",
    "create": "await qs.acreate()",
    "bulk_create": "await qs.abulk_create()",
    "bulk_update": "await qs.abulk_update()",
    "get_or_create": "await qs.aget_or_create()",
    "update_or_create": "await qs.aupdate_or_create()",
    "aggregate": "await qs.aaggregate()",
    "count": "await qs.acount()",
    "exists": "await qs.aexists()",
    "contains": "await qs.acontains()",
    "first": "await qs.afirst()",
    "last": "await qs.alast()",
    "earliest": "await qs.aearliest()",
    "latest": "await qs.alatest()",
    "in_bulk": "await qs.ain_bulk()",
    "iterator": "async for obj in qs.aiterator()",
    "explain": "await qs.aexplain()",
    "delete": "await qs.adelete()",
    "update": "await qs.aupdate()",
    "_iterator": "async for obj in qs.aiterator()",
}
