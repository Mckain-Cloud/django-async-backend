"""Fetch modes for lazy loads in async code.

Django loads a deferred field, a forward foreign key or a reverse one-to-one
on attribute access, through ``instance._state.fetch_mode``. Its modes run
the sync ORM, which raises ``SynchronousOnlyOperation`` inside a task.

``FETCH_BLOCKING`` loads the value on the task's async connection instead,
inside its transaction, by blocking the event loop for that one query (see
``django_async_backend.db.blocking``)::

    from django_async_backend.db.models.fetch_modes import FETCH_BLOCKING

    book = await Book.async_objects.fetch_mode(FETCH_BLOCKING).aget(pk=1)
    book.author.name        # one query; the loop waits for it

Objects loaded through such an instance inherit the mode, so
``book.author.publisher`` works too. In a worker thread (``sync_to_async``)
there is no loop to block and the mode behaves like Django's ``FETCH_ONE``.

Prefer ``await obj.afetch("author")``, ``select_related()`` and
``prefetch_related()`` wherever the code can await: a lazy load in a loop is
an N+1 that stalls every request on the worker, not just this one.
"""

import asyncio
import threading

from django.core.exceptions import SynchronousOnlyOperation
from django.db.models.fetch_modes import (
    FETCH_ONE,
    FetchMode,
    FetchOne,
    FetchPeers,
)
from django.db.models.query_utils import DeferredAttribute

__all__ = ["FETCH_BLOCKING", "FetchBlocking"]

_lazy = threading.local()


def in_lazy_fetch():
    """Is one of Django's own fetch modes loading a value right now?

    ``FETCH_ONE`` loads a deferred field with ``refresh_from_db()``. The
    blocking bridge for that method steps aside here, so a lazy load on
    an instance that did not opt in keeps raising.
    """
    return getattr(_lazy, "depth", 0) > 0


def fetcher_name(fetcher):
    """The attribute a fetcher (one of Django's lazy descriptors) serves."""
    if isinstance(fetcher, DeferredAttribute):
        return fetcher.field.attname
    related = getattr(fetcher, "related", None)
    if related is not None:  # ReverseOneToOneDescriptor
        return related.get_accessor_name()
    return fetcher.field.name


def in_event_loop():
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class FetchBlocking(FetchMode):
    __slots__ = ()

    def fetch(self, fetcher, instance):
        if not in_event_loop():
            return FETCH_ONE.fetch(fetcher, instance)

        from django_async_backend.db.blocking import run_blocking

        run_blocking(
            instance.afetch(fetcher_name(fetcher)), instance._state.db
        )

    def __reduce__(self):
        return "FETCH_BLOCKING"


FETCH_BLOCKING = FetchBlocking()


def _explained(fetch):
    def explained_fetch(self, fetcher, instance):
        _lazy.depth = getattr(_lazy, "depth", 0) + 1
        try:
            return fetch(self, fetcher, instance)
        except SynchronousOnlyOperation:
            if not in_event_loop():
                raise
            name = fetcher_name(fetcher)
            raise SynchronousOnlyOperation(
                "%s.%s is not loaded, and reading it would run a "
                "synchronous query, which async code cannot do. Load it "
                "first with `await obj.afetch(%r)` (or select_related() / "
                "prefetch_related(), or leave it out of defer() / only()). "
                "A queryset with `.fetch_mode(FETCH_BLOCKING)` loads it on "
                "access instead, blocking the event loop for the query."
                % (instance.__class__.__qualname__, name, name)
            ) from None
        finally:
            _lazy.depth -= 1

    explained_fetch._async_backend_explained = True
    return explained_fetch


def patch_fetch_mode_errors():
    """Make Django's lazy-load failure in async code name the way out.

    The exception type stays ``SynchronousOnlyOperation``; only the message
    changes from "use a thread or sync_to_async", which is wrong advice here
    (a thread uses the sync connection, outside the task's transaction).
    """
    for mode in (FetchOne, FetchPeers):
        if not getattr(mode.fetch, "_async_backend_explained", False):
            mode.fetch = _explained(mode.fetch)
