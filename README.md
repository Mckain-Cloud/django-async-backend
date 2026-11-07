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

### Templates

`django_async_backend.template` renders Django templates asynchronously:

```python
from django_async_backend.template import arender_to_string, arender_response

html = await arender_to_string("book.html", {"book": book}, request)
response = await arender_response(TemplateResponse(request, "book.html", ctx))
```

During rendering, `{{ book.author.name }}` loads an unfetched relation with
`afetch()`, `{% for b in author.books.all %}` and `{{ author.books.count }}`
go through the async related manager, async querysets are evaluated when
reached, coroutines (async model methods) are awaited, and `simple_tag`,
`inclusion_tag`, filters and context processors may be `async def`. The
core tags (`for`, `if`, `with`, `block`/`extends`, `include`, `autoescape`,
`spaceless`, `filter`, `ifchanged`) have async implementations; every other
tag renders through its own `render()` after its expressions were resolved
asynchronously. The admin renders every page this way. Set
`ASYNC_BACKEND_ASYNC_TEMPLATES = False` to leave `FilterExpression.resolve`
unpatched (then only `arender*` on templates without custom tags works).

### Sync inputs, hooks and caveats

- A sync `Model.objects` queryset handed to an async form field, formset,
  the changelist (`get_queryset()` overrides) or a template is converted
  with `to_async_queryset()` and runs natively, instead of through Django's
  thread-backed `QuerySet.__aiter__`.
- `Model.aclean()` is awaited by `async_full_clean()` and the async forms
  (`clean()` must not query). `list_display` entries may be `async def`
  methods on the `ModelAdmin` or the model. Custom list filters implement
  `alookups()`. Third-party template nodes define `async def arender()`.
- The async `Query` subclasses Django's, so `filter(fk__in=async_qs)` is
  narrowed to the primary key like a sync subquery.
- `ExclusionConstraint` is validated asynchronously alongside unique and
  check constraints.
- Password hashing in the admin's password-change form runs in a worker
  thread (CPU, no query).
- The admin renders in the view, so `process_template_response` middleware
  sees a rendered response; set `ASYNC_BACKEND_ADMIN_RENDER_IN_VIEW = False`
  to let Django render it (sync engine, worker thread) after those hooks.
- Object-level permissions: `AsyncModelAdmin` and the inlines have
  `ahas_add/change/delete/view_permission()` and `ahas_module_permission()`
  (override these in an async admin). The default answers "model-level
  permission, or the object-level one when an object is given", through the
  backends' async API (`user.ahas_perm(perm, obj)`), so a backend that
  queries per object works. The async views compute every answer Django's
  sync call sites will ask for ahead of time (`aprepare_permissions()`),
  including each related object of a delete confirmation and each row of
  a `list_editable` changelist; the sync `has_*_permission()` methods read
  that per-request cache.

### Custom descriptors

A custom descriptor or manager takes part in `prefetch_related()` by
implementing `async def aget_prefetch_querysets(self, instances, querysets)`
(same six-tuple as Django's hook, with an async queryset) and in `afetch()`
by implementing `async def aget_object(self, instance)`.

### Django REST framework

`django_async_backend.contrib.rest_framework` serves DRF viewsets on the
request's async connection. It builds on [adrf](https://github.com/em1208/adrf)
(async views, viewsets and routers) and replaces every place adrf still hops
to a thread:

```python
from rest_framework import permissions
from django_async_backend.contrib import rest_framework as arf

class BookSerializer(arf.AsyncModelSerializer):
    author_name = serializers.CharField(source="author.name", read_only=True)

    class Meta:
        model = Book
        fields = ["id", "title", "author", "author_name", "tags"]

class BookViewSet(arf.AsyncModelViewSet):
    queryset = Book.objects.select_related("author").prefetch_related("tags")
    serializer_class = BookSerializer
    authentication_classes = [arf.AsyncSessionAuthentication]
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    pagination_class = arf.AsyncPageNumberPagination

router = arf.DefaultRouter()
router.register("books", BookViewSet)
```

- `AsyncModelSerializer`: `ais_valid()`, `adata`, `asave()`.
  `PrimaryKeyRelatedField`/`SlugRelatedField` lookups, `UniqueValidator` and
  `UniqueTogetherValidator` run on the async connection; relations a field
  needs (`source="author.name"`, `StringRelatedField`, nested serializers,
  many-to-many lists) are loaded with `afetch()` first; `acreate()` and
  `aupdate()` use `async_objects` and `async_save()`. Hooks:
  `avalidate_<field>()`, `avalidate()`, and `aget_<name>()` for a
  `SerializerMethodField`.
- `AsyncAPIView`/`AsyncGenericAPIView`: authentication through
  `aauthenticate()` (`AsyncSessionAuthentication` uses `request.auser()`),
  the user's permission cache is warmed so `DjangoModelPermissions` runs no
  query, permissions and throttles are checked in the request's task, and
  `aget_object()` uses `aget()`. Sync querysets on the view or field are
  converted; sync authenticators that query fall back to a thread.
- `AsyncPageNumberPagination` and `AsyncLimitOffsetPagination` count and
  slice asynchronously; `AsyncModelViewSet`, `AsyncReadOnlyModelViewSet` and
  the generic views mirror DRF's, registered with adrf's `DefaultRouter`.
- Not covered: writable nested serializers (as in DRF), filter backends that
  evaluate the queryset, the browsable API's `OPTIONS` metadata (it iterates
  related querysets synchronously).
- `adata` is a plain property returning a coroutine rather than adrf's
  `async_property`, whose descriptor runs `functools.wraps()` on every
  access; on free-threaded CPython 3.15 assigning function attributes
  serialises all threads (hundreds of times slower with two workers), which
  made adrf viewsets slower with more worker threads. DRF's own
  `Field._creation_counter += 1` per field instance is a smaller version of
  the same problem.

### `Model.objects.araw()`

Every Django manager and queryset gets `araw(sql, params)` returning this
package's async `RawQuerySet`, so `[row async for row in
Model.objects.araw("SELECT ...")]` runs on the request's connection
(Django's own `raw()` returns a sync `RawQuerySet` whose `async for` is a
thread hop). `async_objects.raw()` and `async_objects.araw()` are the same.

### The async QuerySet is a Django QuerySet

`django_async_backend.db.models.query.QuerySet` subclasses
`django.db.models.QuerySet`, and `AsyncManager` subclasses `Manager`, so
`isinstance()` checks in Django and third-party code (REST framework's
related fields, django-filter, forms) take the queryset branch. The sync
API that inheritance would re-expose (`get()`, `count()`, `exists()`,
`delete()`, `update()`, `first()`, `iterator()`, ... and truth-testing,
`len()`, `iter()`, pickling before evaluation) raises a `TypeError` naming
the async twin instead of quietly running the query on the sync connection.

### Caches

`django_async_backend.core.cache.AsyncDatabaseCache` is Django's database
cache backend with the `a*` methods implemented on the async connection
(`INSERT ... ON CONFLICT` upserts, async culling); the sync methods and
`createcachetable` are unchanged. Django's `BaseCache` implements every
async method as a thread hop, so one native backend lifts everything built
on the cache API:

```python
CACHES = {"default": {
    "BACKEND": "django_async_backend.core.cache.AsyncDatabaseCache",
    "LOCATION": "cache_table",
}}
SESSION_ENGINE = "django.contrib.sessions.backends.cache"   # or cached_db
```

- Django's cache and `cached_db` session stores are async by composition.
- `{% cache %}` is rendered by the async template engine with `aget`/`aset`.
- `django_async_backend.core.cache.acache_page(timeout)` and
  `AsyncCacheMiddleware` are the async `cache_page()` / `CacheMiddleware`
  (with `aget_cache_key()` / `alearn_cache_key()` for the header registry).
- REST framework: `AsyncSimpleRateThrottle`, `AsyncAnonRateThrottle`,
  `AsyncUserRateThrottle`, `AsyncScopedRateThrottle` await the cache.

The rows live in the request's database and the operations run on its
connection, so a cache write inside `async_atomic()` rolls back with the
transaction, as Django's own `DatabaseCache` does under `ATOMIC_REQUESTS`.

### Finding what still runs synchronously

Three settings make the remaining sync paths visible or forbidden:

- **System checks.** `async_backend.W001`: a model overrides `save()`,
  `delete()` or `clean()` without `async_save()`, `async_delete()` or
  `aclean()`; the async methods are generated from Django's bodies and skip
  the sync override. `async_backend.W002`: a sync receiver is connected to a
  request-time signal (`pre_save`, `post_save`, `pre_delete`, `post_delete`,
  `m2m_changed`, the auth login signals). `ASYNC_BACKEND_OVERRIDE_CHECK` and
  `ASYNC_BACKEND_SYNC_RECEIVER_CHECK` take `"warn"` (default), `"error"` or
  `"off"`. Django's own `AbstractBaseUser.save()` and `AbstractUser.clean()`
  overrides, and `authtoken.Token.save()`, get async twins from the backend.
  `async_backend.W003` does the same for `AsyncModelAdmin` subclasses that
  override `save_model()`, `delete_model()`, `save_formset()` or
  `save_related()` instead of the `a`-prefixed hooks the async admin calls.
- **`Model.aclean_fields()`** mirrors `clean_fields()` with the foreign-key
  existence check on the async connection; `async_full_clean()` uses it.
- **Sync receivers inside a transaction.** `Signal.asend()` runs a sync
  receiver in a worker thread on the sync connection, where it cannot see
  the uncommitted row it was told about. `ASYNC_BACKEND_SYNC_RECEIVERS`:
  `"warn"` (default) emits `SyncReceiverWarning` once per receiver fired
  inside `async_atomic()`, `"error"` raises `SyncReceiverInTransaction`,
  `"thread"` keeps Django's behaviour quietly. Make the receiver `async def`
  and it runs in the sender's task instead.
- **Any sync ORM hop.** `ASYNC_BACKEND_SYNC_HOPS = "warn"` logs, once per
  call site with the application frames of its stack, every sync database
  cursor opened in a `sync_to_async()` thread while an event loop drives the
  request (logger `django_async_backend.hops`); `"error"` raises
  `SyncHopError` there. Default `"off"`.
- **Template tags that query.** A `simple_tag`, `inclusion_tag` or custom
  node whose own code hits the sync ORM is run in a worker thread with an
  `AsyncTemplateHopWarning` once per node class;
  `ASYNC_BACKEND_TEMPLATE_SYNC_NODES = "error"` re-raises instead.

For REST framework the sync fallbacks have async replacements:
`AsyncBasicAuthentication`, `AsyncTokenAuthentication`,
`AsyncRemoteUserAuthentication`, `AsyncDjangoModelPermissions` and
`AsyncDjangoObjectPermissions` (object-level checks through
`user.ahas_perms(perms, obj)`, so a backend with `ahas_perm()` runs on the
request's connection). `django_async_backend.contrib.django_filters` provides
`AsyncFilterSet`, `AsyncModelChoiceFilter`, `AsyncModelMultipleChoiceFilter`
and `AsyncDjangoFilterBackend` (`filterset_class` or `filterset_fields` as
usual; the related-field validation and the filtering run asynchronously).
`django_async_backend.forms.AsyncForm` gives a plain form `ais_valid()`.

### Free-threaded Python

The library and its test suite run on CPython 3.15t with the GIL disabled
(psycopg-binary, uvloop and pydantic-core all declare free-threading
support). Two things are specific to that build:

- Granian's free-threaded build runs each worker as a *thread* with its own
  event loop in one process. Connection pools are therefore registered per
  event loop (an `AsyncConnectionPool` is bound to the loop that opened it);
  the sync wrapper's pool stays process-wide as in Django.
- The free-threaded cyclic collector is non-generational and stops every
  thread. With several busy worker threads a collection takes hundreds of
  milliseconds and runs often, which shows up as p99 latency; raising
  `gc.set_threshold()` trades fewer, longer pauses, and `gc.freeze()` does
  not shorten them. Per-process workers do not have this problem: granian
  rebuilt with its process model on the free-threaded interpreter (its
  worker model is a compile-time choice keyed on `Py_GIL_DISABLED`) matched
  or beat the GIL build on every endpoint, at the price of one full
  interpreter per worker. Watch `gc.callbacks` timings before choosing
  thread workers.
