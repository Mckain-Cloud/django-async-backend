"""Sync queryset evaluation in async code, for ``FETCH_BLOCKING`` querysets.

With ``ASYNC_BACKEND_BLOCKING_QUERYSETS = True``, a queryset whose fetch mode
is ``FETCH_BLOCKING`` can be used through the sync API from inside a task:
the query runs on the task's async connection, in its transaction, with the
event loop blocked until it returns (``django_async_backend.db.blocking``).

* The async ``QuerySet``: ``for obj in qs``, ``len(qs)``, ``if qs``,
  ``obj in qs`` and the sync-only methods (``count()``, ``exists()``,
  ``get()``, ``first()``, ``update()``, ``delete()``...) run their async twin.
* Django's own ``QuerySet`` (``Model.objects``, ``obj.<related manager>``):
  evaluation and the same methods are redirected to the async queryset
  (``to_async_queryset()``) when an event loop is running in the thread.

Django's related managers copy the fetch mode of the instance they hang off,
so ``obj.books.all()`` is covered once ``obj`` came from a ``FETCH_BLOCKING``
queryset. Every other queryset behaves as before, and so does a worker thread
(``sync_to_async``), where there is no loop to block.

Writes are explicit calls rather than something a template can trigger by
accident, and a new instance has no queryset to inherit a mode from, so with
the setting on they block for any object, whenever an event loop is running
in the thread:

* ``Model.save()`` / ``Model.delete()`` run Django's ``async_save()`` /
  ``async_delete()`` body after the model's own sync overrides, so a model's
  ``async_save()`` override is *not* called on top of its ``save()``.
* Related managers' ``add()`` / ``remove()`` / ``clear()`` / ``set()`` run the
  ``async_objects`` twins (wired in ``related.bridge_related_manager``).
* ``transaction.atomic()`` enters ``async_atomic()`` on the task's
  connection, and ``transaction.on_commit()`` registers there.
* Sync signal receivers fired by such a write are called inline, in the
  transaction (``django_async_backend.dispatch``).

Not covered: ``iterator()``, ``refresh_from_db()``, model validation
(``full_clean()`` / ``validate_unique()``), ``select_for_update()`` on a
queryset without the mode, raw ``connection.cursor()``, and the rest of
``django.db.transaction`` (``savepoint()``, ``set_rollback()``...). Indexing
an *async* queryset (``qs[0]``) still returns an awaitable.
"""

from contextvars import ContextVar

from django.db import (
    DEFAULT_DB_ALIAS,
    router,
    transaction,
)
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models import Model
from django.db.models import query as django_query

from django_async_backend.db import async_connections
from django_async_backend.db.blocking import run_blocking
from django_async_backend.db.models import query as async_query
from django_async_backend.db.models.convert import to_async_queryset
from django_async_backend.db.models.fetch_modes import (
    FetchBlocking,
    in_event_loop,
)
from django_async_backend.db.models.sync_only import SYNC_ONLY
from django_async_backend.db.transaction import async_atomic

# Streaming needs a suspended generator between rows; not served blocking.
_UNBRIDGED = {"iterator", "_iterator"}
_TWINS = {name: "a" + name for name in SYNC_ONLY if name not in _UNBRIDGED}
_METHOD_ATTRS = ("alters_data", "queryset_only", "__doc__")

_saved = []
# (Atomic, AsyncAtomic, alias) for each sync atomic() this task has open.
_atomics = ContextVar("async_backend_blocking_atomics", default=())


def writes_block():
    """Should a sync write in this thread run on the async connection?"""
    return bool(_saved) and in_event_loop()


def is_blocking_instance(instance):
    return (
        bool(_saved)
        and isinstance(instance._state.fetch_mode, FetchBlocking)
        and in_event_loop()
    )


def is_blocking(queryset):
    return (
        isinstance(getattr(queryset, "_fetch_mode", None), FetchBlocking)
        and in_event_loop()
    )


def _evaluated(queryset):
    if queryset._result_cache is None:
        run_blocking(queryset._fetch_all(), queryset.db)
    return queryset._result_cache


def _patch(cls, name, new):
    original = cls.__dict__.get(name)
    for attr in _METHOD_ATTRS:
        if hasattr(original, attr):
            setattr(new, attr, getattr(original, attr))
    new.__name__ = name
    _saved.append((cls, name, original))
    setattr(cls, name, new)


def _async_dunder(name, serve):
    original = async_query.QuerySet.__dict__[name]

    def dunder(self, *args):
        if is_blocking(self):
            return serve(_evaluated(self), *args)
        return original(self, *args)

    return dunder


def _async_contains(self, obj):
    if is_blocking(self):
        return obj in _evaluated(self)
    raise TypeError(
        "`in` is not supported on %s. Use `await qs.acontains(obj)`."
        % type(self).__name__
    )


def _async_method(name, twin):
    stub = async_query.QuerySet.__dict__[name]

    def method(self, *args, **kwargs):
        if is_blocking(self):
            return run_blocking(getattr(self, twin)(*args, **kwargs), self.db)
        return stub(self, *args, **kwargs)

    return method


def _sync_fetch_all(original):
    def _fetch_all(self):
        if self._result_cache is None and is_blocking(self):
            queryset = to_async_queryset(self)
            if queryset is not self:
                run_blocking(queryset._fetch_all(), queryset.db)
                self._result_cache = queryset._result_cache
                self._prefetch_done = True
                return
        return original(self)

    return _fetch_all


def _sync_method(original, twin):
    def method(self, *args, **kwargs):
        if self._result_cache is None and is_blocking(self):
            queryset = to_async_queryset(self)
            if queryset is not self:
                return run_blocking(
                    getattr(queryset, twin)(*args, **kwargs), queryset.db
                )
        return original(self, *args, **kwargs)

    return method


def _model_save(original):
    def save(self, *args, **kwargs):
        if not in_event_loop():
            return original(self, *args, **kwargs)
        using = kwargs.get("using") or router.db_for_write(
            self.__class__, instance=self
        )
        kwargs["using"] = using
        # Model.async_save, not self.async_save: the sync overrides have
        # just run, and an async override would repeat their work.
        return run_blocking(Model.async_save(self, *args, **kwargs), using)

    return save


def _model_delete(original):
    def delete(self, using=None, keep_parents=False):
        if not in_event_loop():
            return original(self, using=using, keep_parents=keep_parents)
        using = using or router.db_for_write(self.__class__, instance=self)
        return run_blocking(
            Model.async_delete(self, using=using, keep_parents=keep_parents),
            using,
        )

    return delete


def _atomic_enter(original):
    def __enter__(self):
        if not in_event_loop():
            return original(self)
        alias = self.using or DEFAULT_DB_ALIAS
        block = async_atomic(
            using=alias, savepoint=self.savepoint, durable=self.durable
        )
        run_blocking(block.__aenter__(), alias)
        _atomics.set(_atomics.get() + ((self, block, alias),))

    return __enter__


def _atomic_exit(original):
    def __exit__(self, exc_type, exc_value, traceback):
        stack = _atomics.get()
        if not stack or stack[-1][0] is not self:
            return original(self, exc_type, exc_value, traceback)
        _, block, alias = stack[-1]
        _atomics.set(stack[:-1])
        return run_blocking(
            block.__aexit__(exc_type, exc_value, traceback), alias
        )

    return __exit__


def _on_commit(original):
    def on_commit(self, func, robust=False):
        if not in_event_loop():
            return original(self, func, robust)
        return run_blocking(
            async_connections[self.alias].on_commit(func, robust), self.alias
        )

    return on_commit


def install():
    if _saved:
        return
    async_cls = async_query.QuerySet
    sync_cls = django_query.QuerySet

    _patch(async_cls, "__iter__", _async_dunder("__iter__", iter))
    _patch(async_cls, "__len__", _async_dunder("__len__", len))
    _patch(async_cls, "__bool__", _async_dunder("__bool__", bool))
    _patch(async_cls, "__contains__", _async_contains)
    for name, twin in _TWINS.items():
        _patch(async_cls, name, _async_method(name, twin))

    _patch(sync_cls, "_fetch_all", _sync_fetch_all(sync_cls._fetch_all))
    for name, twin in _TWINS.items():
        original = sync_cls.__dict__.get(name)
        if original is not None:
            _patch(sync_cls, name, _sync_method(original, twin))

    _patch(Model, "save", _model_save(Model.save))
    _patch(Model, "delete", _model_delete(Model.delete))
    _patch(
        transaction.Atomic,
        "__enter__",
        _atomic_enter(transaction.Atomic.__enter__),
    )
    _patch(
        transaction.Atomic,
        "__exit__",
        _atomic_exit(transaction.Atomic.__exit__),
    )
    _patch(
        BaseDatabaseWrapper,
        "on_commit",
        _on_commit(BaseDatabaseWrapper.on_commit),
    )


def uninstall():
    while _saved:
        cls, name, original = _saved.pop()
        if original is None:
            delattr(cls, name)
        else:
            setattr(cls, name, original)
