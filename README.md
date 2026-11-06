# Async Django ORM (Django Async Backend)

[![CI status](https://img.shields.io/github/actions/workflow/status/Arfey/django-async-backend/test.yml?logo=github&style=for-the-badge&labelColor=%23282828)](https://github.com/Arfey/django-async-backend/actions)
[![Latest Version in PyPI](https://img.shields.io/pypi/v/django-async-backend.svg?style=for-the-badge)](https://pypi.org/project/django-async-backend/)
[![Supported Python versions](https://img.shields.io/pypi/pyversions/django-async-backend.svg?style=for-the-badge)](https://pypi.org/project/django-async-backend/)

[![Monthly downloads](https://static.pepy.tech/personalized-badge/django-async-backend?period=month&units=international_system&left_color=grey&right_color=blue&left_text=downloads/month)](https://pepy.tech/projects/django-async-backend)

Async Django ORM and PostgreSQL database backend.

Django's `a`-prefixed ORM methods (`aget`, `acreate`, …) are thin
`sync_to_async` wrappers: the query still runs on a threadpool, on a
synchronous connection. **django-async-backend** replaces the database layer
itself, so queries are issued on a real asyncio connection through
[psycopg](https://www.psycopg.org/psycopg3/) 3, with async transactions, async
cursors and optional connection pooling — no thread emulation.

The project is **production ready**: the API is stable, and each release is
pinned to the Django feature release it was generated against, so upgrades stay
predictable.

📖 **[Read the documentation](https://django-async-backend.readthedocs.io/en/latest/)**

---

## Compatibility

The package tracks Django's major and minor version, so the release you install
is pinned to the Django feature release it was generated against.

| django-async-backend | Django  |
| -------------------- | ------- |
| `6.1.3`              | `6.1.0` |

Part of the ORM layer is generated from Django's own source, so a Django feature
release gets a matching django-async-backend feature release rather than a
loosened version range. Patch releases within a line are ordinary bugfix
releases and are safe to upgrade to.

> [!IMPORTANT]
> **Run this under ASGI.** django-async-backend is developed for ASGI, and that
> is the only mode it is supported in. Under WSGI — including the Django
> development server — it behaves inconsistently, because WSGI creates a new
> event loop for each request and the async connection state cannot be managed
> reliably across them. Connection pooling in particular is not supported
> there.

## Installation

```bash
pip install django-async-backend[binary]
```

The `binary` extra installs the C-accelerated `psycopg` implementation. Without
it you get the pure-Python implementation, which is noticeably slower. If you
use connection pooling, add the `pool` extra as well:

```bash
pip install django-async-backend[binary,pool]
```

The package tracks Django's major and minor version — for example `6.0.x`
matches Django `6.0` — because a large part of the ORM layer is generated from
Django's own source.

## Quick start

```python
# settings.py
DATABASES = {
    "default": {
        "ENGINE": "django_async_backend.db.backends.postgresql",
        ...
    },
}

INSTALLED_APPS = [
    ...
    "django_async_backend",
]

# Required under ASGI so connections are released each request.
MIDDLEWARE = [
    "django_async_backend.middleware.close_async_connections",
    ...
]
```

```python
from django.db import models
from django_async_backend.db import async_acquire
from django_async_backend.db.models.base import AsyncModelMixin
from django_async_backend.db.transaction import async_atomic


class Book(AsyncModelMixin, models.Model):
    name = models.CharField(max_length=100)


async def notify(book_id: int) -> None:
    ...


async def main() -> None:
    async with async_acquire() as connection:
        async with async_atomic():
            book = await Book.async_objects.acreate(name="Django")

            # async callbacks are supported; runs only if the transaction commits
            await connection.on_commit(lambda: notify(book.pk))

            book.name = "Django Async"
            await book.async_save(update_fields=["name"])

            async with async_atomic():  # savepoint
                await Book.async_objects.filter(name="draft").adelete()

        print(await Book.async_objects.acount())

        async for row in Book.async_objects.order_by("name"):
            print(row.pk, row.name)
```

Or drop to a raw async cursor:

```python
async with await connection.cursor() as cursor:
    await cursor.execute("SELECT id, name FROM app_book ORDER BY name")

    print(cursor.rowcount)

    async for row in cursor:
        print(row)
```

> [!WARNING]
> **Async does not mean parallel.** A task gets one connection per database
> alias, and every ORM and cursor call in that task takes turns on it — so
> awaiting several queries in a row does not make them run concurrently.
>
> The connection is owned by the task that created it, so you cannot fan out
> onto it either: using it from another task — `asyncio.create_task()`,
> `asyncio.gather()`, `asyncio.TaskGroup` — raises `RuntimeError`. Wrapping the
> fan-out in a single `async_atomic()` block does **not** make it safe. To run
> queries in parallel, wrap each task's work in `async_acquire()`, which opens
> a connection for a task that doesn't own one. Do it sparingly, since each
> such connection is a real one and a wide fan-out can exhaust the server's
> limit.

## Supported methods

Legend: ✅ supported · ❌ not supported · ⚠️ supported with caveats

### QuerySet methods

| methods                             | supported | comments |
| ----------------------------------- | --------- | -------- |
| `Model.objects.aget`                | ✅        |          |
| `Model.objects.acreate`             | ✅        |          |
| `Model.objects.acount`              | ✅        |          |
| `Model.objects.none`                | ✅        |          |
| `Model.objects.abulk_create`        | ✅        |          |
| `Model.objects.abulk_update`        | ✅        |          |
| `Model.objects.aget_or_create`      | ✅        |          |
| `Model.objects.aupdate_or_create`   | ✅        |          |
| `Model.objects.aearliest`           | ✅        |          |
| `Model.objects.alatest`             | ✅        |          |
| `Model.objects.afirst`              | ✅        |          |
| `Model.objects.alast`               | ✅        |          |
| `Model.objects.ain_bulk`            | ✅        |          |
| `Model.objects.adelete`             | ✅        |          |
| `Model.objects.aupdate`             | ✅        |          |
| `Model.objects.aexists`             | ✅        |          |
| `Model.objects.acontains`           | ✅        |          |
| `Model.objects.aexplain`            | ✅        |          |
| `Model.objects.araw`                | ❌        |          |
| `Model.objects.all`                 | ✅        |          |
| `Model.objects.filter`              | ✅        |          |
| `Model.objects.exclude`             | ✅        |          |
| `Model.objects.complex_filter`      | ✅        |          |
| `Model.objects.union`               | ✅        |          |
| `Model.objects.intersection`        | ✅        |          |
| `Model.objects.difference`          | ✅        |          |
| `Model.objects.select_related`      | ✅        |          |
| `Model.objects.select_for_update`   | ✅        |          |
| `Model.objects.prefetch_related`    | ✅        | Includes `GenericForeignKey` and `GenericRelation` |
| `Model.objects.aaggregate`          | ✅        |          |
| `Model.objects.annotate`            | ✅        |          |
| `Model.objects.order_by`            | ✅        |          |
| `Model.objects.distinct`            | ✅        |          |
| `Model.objects.extra`               | ✅        |          |
| `Model.objects.reverse`             | ✅        |          |
| `Model.objects.defer`               | ⚠️        | reading a deferred field in async code raises `SynchronousOnlyOperation`; load it with `await obj.afetch("field")` |
| `Model.objects.only`                | ⚠️        | same caveat as `defer` |
| `Model.objects.using`               | ✅        |          |
| `Model.objects.resolve_expression`  | ✅        |          |
| `Model.objects.ordered`             | ✅        |          |
| `Model.objects.values`              | ✅        |          |
| `Model.objects.values_list`         | ✅        |          |
| `Model.objects.dates`               | ✅        |          |
| `Model.objects.datetimes`           | ✅        |          |
| `Model.objects.alias`               | ✅        |          |
| `Model.objects.aiterator`           | ✅        | server-side cursor; closed as soon as the consumer stops iterating |
| `Model.objects.raw`                 | ✅        | returns the async `RawQuerySet` below |

`GenericForeignKey` prefetches run one target query per content type,
sequentially on the task's connection. ContentType cache misses are resolved
asynchronously. For custom prefetches, use
`django_async_backend.db.models.query.Prefetch` with an async queryset.

### Dunder methods

| methods            | supported | comments |
| ------------------ | --------- | -------- |
| `__aiter__`        | ✅        |          |
| `__iter__`         | ⚠️        | raises `TypeError` — use `async for obj in qs` |
| `__len__`          | ⚠️        | raises `TypeError` — use `await qs.acount()` |
| `__contains__`     | ⚠️        | falls back to `__iter__`, so it raises `TypeError` too |
| `__bool__`         | ⚠️        | truth-testing falls back to `__len__`, so `if qs:` raises `TypeError` — use `await qs.aexists()` |
| `__repr__`         | ✅        |          |
| `__and__`          | ✅        |          |
| `__or__`           | ✅        |          |
| `__xor__`          | ✅        |          |
| `__getitem__`      | ✅        |          |

### Model methods

Django's own `a*` methods keep their existing behavior; the genuinely async
equivalents are the names in the comments column.

| methods                  | supported | comments                 |
| ------------------------ | --------- | ------------------------ |
| `Model.asave`            | ✅        | `async_save`             |
| `Model.adelete`          | ✅        | `async_delete`           |
| `Model.arefresh_from_db` | ✅        | `async_refresh_from_db`  |
| `Model.full_clean`       | ✅        | `async_full_clean` (also `async_validate_unique`, `async_validate_constraints`); unique and check constraints are validated on the async connection |
| `Model.afetch`           | ✅        | new: `await obj.afetch("author", "body", "tags")` loads forward FKs, reverse one-to-ones, deferred fields and multi-valued relations, filling Django's caches so the plain attribute works afterwards |

### RawQuerySet

Supported ✅ — `Model.async_objects.raw(sql, params)` returns an async
`RawQuerySet`: iterate it with `async for` (cached) or `.aiterator()`
(streamed). `len()`, `bool()`, indexing and sync iteration raise `TypeError`.

### Related managers

Supported ✅ through `instance.<related>.async_objects`, on reverse foreign
keys and both sides of many-to-many fields:

```python
async for book in author.books.async_objects.order_by("title"):
    ...
await author.books.async_objects.acreate(title="New")
await book.tags.async_objects.aadd(tag)      # native: same transaction
await book.tags.async_objects.aset([t1, t2])
```

`aadd`/`aremove`/`aclear`/`aset`/`acreate`/`aget_or_create`/
`aupdate_or_create` run on the task's connection and send `m2m_changed`
with `asend()`. The plain `instance.<related>` manager (and its own `a*`
methods) is still Django's thread-backed sync ORM. Generic relations are not
covered.

### Signals

`Signal.asend()` normally runs async receivers concurrently, each in its own
task, which cannot use the sender's connection. The backend replaces that
runner on startup so async receivers run **sequentially in the sender's
task**, on its connection and inside its transaction, like sync receivers do
under WSGI. Set `ASYNC_BACKEND_SEQUENTIAL_SIGNAL_RECEIVERS = False` to keep
Django's parallel behaviour. Sync receivers are still dispatched through
`sync_to_async()` on the *sync* connection, outside the async transaction:
inside `async_atomic()`, receivers that touch the database must be `async`.

### Forms

`django_async_backend.forms` has `AsyncModelForm`, `AsyncModelChoiceField`,
`AsyncModelMultipleChoiceField`, async model/inline formsets and the matching
factories. Validation (`ais_valid()`, unique and constraint checks), saving
(`asave()`, `asave_m2m()`) and choice loading (`aload_choices()`) run on the
async connection; the sync methods keep working for sync views.

### Admin, auth, sessions, messages

`django_async_backend.contrib` carries the rest of the request path:

| piece | setting |
| --- | --- |
| `contrib.auth.AsyncModelBackend` | `AUTHENTICATION_BACKENDS` |
| `contrib.sessions` (`SessionStore`) | `SESSION_ENGINE` |
| `contrib.sessions.AsyncSessionMiddleware` | replaces `SessionMiddleware` |
| `contrib.messages.AsyncMessageMiddleware` | replaces `MessageMiddleware` |
| `contrib.admin.AsyncAdminSite` / `AsyncModelAdmin` / inlines | your admin site |

With all of them in place, logging in, the admin index, changelists with
filters, search and pagination, add/change forms with inlines and
many-to-many fields, delete confirmations with cascades, actions, history,
autocomplete, raw-id widgets, list-editable rows, password change and logout
run entirely on the task's async connection; the end-to-end tests run every
request under a guard that fails on any synchronous cursor. Templates are
still rendered synchronously (in the view's task) from pre-evaluated context,
so a lazy query inside a template raises instead of running on a thread.
`date_hierarchy` and facet counts are precomputed by the async changelist.
Custom list filters subclass `AsyncSimpleListFilter` and implement
`alookups()`. Generic relations are covered too: `instance.<generic
relation>.async_objects` managers, `afetch()` of a `GenericForeignKey`, and
`AsyncGenericStackedInline` / `AsyncGenericTabularInline`. The parent's
`ContentType` must be in Django's cache before a generic manager is built
(`await aget_for_model(Model)`); the admin does that itself.

### Custom descriptors

A custom descriptor or manager takes part in `prefetch_related()` by
implementing `async def aget_prefetch_querysets(self, instances, querysets)`
(same six-tuple as Django's hook, with an async queryset) and in `afetch()`
by implementing `async def aget_object(self, instance)`.
