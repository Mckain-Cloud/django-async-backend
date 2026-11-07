# Writing async tests

The test cases in `django_async_backend.test` mirror Django's, on the async
connection.

## AsyncioTestCase

Like Django's `TestCase`: the class opens one event loop and one database
transaction, loads `fixtures` and `asetUpTestData()` inside it once, and
wraps every test in a savepoint that is rolled back afterwards. Use it for
nearly everything; it is the fast one.

```python
from django_async_backend.test import AsyncioTestCase


class BookTests(AsyncioTestCase):
    fixtures = ["authors"]

    @classmethod
    async def asetUpTestData(cls):
        cls.book = await Book.async_objects.acreate(title="Dune")

    async def asyncSetUp(self):
        # Per-test setup, inside the test's savepoint.
        ...

    async def test_title(self):
        self.assertEqual(self.book.title, "Dune")
        async with self.assertNumQueries(1):
            self.assertEqual(await Book.async_objects.acount(), 1)
```

Attributes assigned in `asetUpTestData()` are deep-copied for each test, so a
test may modify `self.book` without affecting the next test. Deferrable
constraints are checked before the rollback, so a violation fails the test
instead of disappearing with it.

`databases = {"default"}` restricts the transactions to some aliases; the
default is every async alias.

## AsyncioTransactionTestCase

Like Django's `TransactionTestCase`: autocommit connections, `fixtures`
loaded before every test, and every table flushed after it. Use it when the
code under test manages transactions itself.

```python
from django_async_backend.test import AsyncioTransactionTestCase


class CommitTests(AsyncioTransactionTestCase):
    async def test_commit(self):
        async with async_atomic():
            await do_db_stuff()
```

## Assertions

- `async with self.assertNumQueries(n, using="default"):` or
  `await self.assertNumQueries(n, coroutine_function, *args)`.
- `await self.assertQuerySetEqual(qs, values, transform=None, ordered=True)`.
- `async with self.captureOnCommitCallbacks(execute=True) as callbacks:`
  captures `on_commit()` callbacks registered on the async connection and,
  with `execute=True`, runs (and awaits) them when the block ends.
- `AsyncCaptureQueriesContext(connection)` is the building block behind
  `assertNumQueries`.

## Fixtures

`aload_fixtures(labels, using="default")` loads Django fixtures on the
async connection. Discovery and deserialization are Django's own; the objects
are saved with `async_save_base(raw=True)` so they land in the caller's
transaction, and sequences are reset afterwards. Natural keys are not
supported (resolving them queries through the sync manager).

## Task ownership

An async connection belongs to the asyncio task that first used it, and test
harnesses run setup, the test body and teardown as different tasks. The test
cases above re-own the connections at every phase. A harness of your own
calls `release_task_ownership()` when a phase ends, so the next task can
claim the connections.

The request-closing middleware does not close a connection that is inside a
test-case transaction, so `AsyncClient` requests made from a test see the
test's data (Django's test client disconnects `close_old_connections` for
the same reason).

## pytest

Installing the package registers a pytest plugin. It needs pytest-django
(test database, settings) and pytest-asyncio (the event loop the fixtures
and the test share; keep the fixture loop scope and the test loop scope
equal, which the defaults are).

```python
import pytest


async def test_create(adb):
    await Book.async_objects.acreate(title="Dune")
    assert await Book.async_objects.acount() == 1  # rolled back afterwards


@pytest.mark.async_db(transaction=True)
async def test_commit():
    ...


async def test_admin(aadmin_client, adjango_assert_num_queries):
    async with adjango_assert_num_queries(3):
        response = await aadmin_client.get("/admin/")
    assert response.status_code == 200
```

| Fixture | Mirrors |
| --- | --- |
| `adb` | pytest-django `db` (transaction, rolled back) |
| `atransactional_db` | pytest-django `transactional_db` (flush) |
| `@pytest.mark.async_db(transaction=False, databases=None)` | `@pytest.mark.django_db` |
| `aclient`, `aadmin_user`, `aadmin_client` | `client`, `admin_user`, `admin_client` (as `AsyncClient`) |
| `adjango_assert_num_queries`, `adjango_assert_max_num_queries` | the pytest-django equivalents, as async context managers |
| `adjango_capture_on_commit_callbacks` | `django_capture_on_commit_callbacks` |
| `async_db_blocker` | `django_db_blocker` |

An async query in a test that did not ask for the database raises
`RuntimeError`, like pytest-django's blocker. `AsyncioTestCase` classes
collected by pytest get the test database and both blockers lifted
automatically.

On Windows the plugin switches to the selector event loop policy, which
psycopg's async connection needs; set
`django_async_backend_selector_loop = false` in the pytest configuration to
keep the default.

## factory_boy

```python
import factory
from django_async_backend.contrib.factory_boy import (
    AsyncDjangoModelFactory, AsyncIterator, async_post_generation,
)


class BookFactory(AsyncDjangoModelFactory):
    class Meta:
        model = Book
        django_get_or_create = ("title",)

    title = factory.Sequence(lambda n: f"Book {n}")
    author = factory.SubFactory(AuthorFactory)
    publisher = AsyncIterator(lambda: Publisher.objects.all())

    @async_post_generation
    async def tags(self, create, extracted, **kwargs):
        if create and extracted:
            await self.tags.async_objects.aset(extracted)


book = await BookFactory.acreate(tags=[tag])
books = await BookFactory.acreate_batch(3)
unsaved = await BookFactory.abuild()
```

`acreate()` runs the ordinary `build()`, which produces the whole object
graph without touching the database, and then saves it with `async_save()`,
dependencies first. `SubFactory`, `RelatedFactory`, `Trait`,
`django_get_or_create` (on sub-factories as well), `Meta.database` and sync
`post_generation` hooks that stay off the database all work. Hooks that use
the database are written as `@async_post_generation` coroutines and run after
the save; `Iterator` over a queryset becomes `AsyncIterator`, which
`acreate()` evaluates on the async connection before building.

## model_bakery

```python
from django_async_backend.contrib import model_bakery as abaker

book = await abaker.amake(Book, title="Dune")
books = await abaker.amake(Book, _quantity=3, _fill_optional=True)
owner = await abaker.amake(Owner, tags=[tag], make_m2m=True)
author = await abaker.amake_recipe("catalog.author_with_books")
```

`baker.prepare()` builds the instance and its generated related instances
unsaved; `amake()` then saves the graph with `async_save()` and applies the
values model_bakery applies after save: m2m values, reverse one-to-many and
one-to-one values, generic foreign keys to saved instances, auto_now
overrides, `_refresh_after_create`. Recipes work through `amake_recipe()`
with `foreign_key()`, `related()`, `seq()` and `cycle()`. `aprepare()` and
`aprepare_recipe()` are model_bakery's own `prepare` functions.

## Overriding settings

Django's `@override_settings` only accepts `SimpleTestCase` subclasses, so it
raises `ValueError` on the async test cases. Import it from
`django_async_backend.test` instead — same API, and it also works on Django's
own test cases, so a module can use the one import for both:

```python
from django_async_backend.test import AsyncioTestCase, override_settings


@override_settings(SOME_SETTING="value")
class MyAsyncTests(AsyncioTestCase):
    async def test_something(self):
        ...
```

`modify_settings` is available from the same place.

To override a setting for part of a test only, use `self.settings()` or
`self.modify_settings()` as a context manager:

```python
class MyAsyncTests(AsyncioTestCase):
    async def test_something(self):
        with self.settings(SOME_SETTING="value"):
            ...
```

## Running the test suite

See [Contributing](contribute.md) for running this project's own tests.
