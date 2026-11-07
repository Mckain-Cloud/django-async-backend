"""pytest plugin: async database fixtures for pytest-django + pytest-asyncio.

Registered automatically through the ``pytest11`` entry point. Requires
pytest-django (test database creation, settings) and pytest-asyncio (the
event loop the fixtures and tests share).

Fixtures
--------
``adb``
    Open a transaction on every async connection and roll it back after the
    test (Django's TestCase model). Also enabled by ``@pytest.mark.async_db``.
``atransactional_db``
    Autocommit connections, tables flushed after the test (Django's
    TransactionTestCase model). Also ``@pytest.mark.async_db(transaction=True)``.
``aclient`` / ``aadmin_client``
    ``django.test.AsyncClient``, the latter logged in as ``aadmin_user``.
``aadmin_user``
    A superuser created with the async ORM (username ``admin``, password
    ``password``).
``adjango_assert_num_queries`` / ``adjango_assert_max_num_queries``
    ``async with adjango_assert_num_queries(2): ...``
``adjango_capture_on_commit_callbacks``
    ``async with adjango_capture_on_commit_callbacks(execute=True) as cbs:``
``async_db_blocker``
    The blocker that refuses async connections in tests that did not ask
    for the database (``unblock()`` / ``block()`` context managers).

Keep the fixture loop scope and the test loop scope equal (the defaults are
both ``function``): the connection opened in the fixture is bound to that
loop.

On Windows the plugin switches to the selector event loop policy, which
psycopg's async connection requires; set
``django_async_backend_selector_loop = false`` in the pytest config to keep
the default policy.
"""

import asyncio
import contextlib
import sys
import warnings

import pytest

try:
    import pytest_asyncio

    afixture = pytest_asyncio.fixture
except ImportError:  # pragma: no cover
    afixture = pytest.fixture

_BLOCKED_MESSAGE = (
    "Async database access not allowed, use the 'async_db' mark, or the "
    "'adb' or 'atransactional_db' fixtures to enable it."
)


class AsyncDbBlocker:
    """Manager for ``BaseAsyncDatabaseWrapper.ensure_connection``."""

    def __init__(self):
        self._history = []
        self._real_ensure_connection = None

    @property
    def _wrapper_class(self):
        from django_async_backend.db.backends.base.base import (
            BaseAsyncDatabaseWrapper,
        )

        if self._real_ensure_connection is None:
            self._real_ensure_connection = (
                BaseAsyncDatabaseWrapper.ensure_connection
            )
        return BaseAsyncDatabaseWrapper

    @staticmethod
    async def _blocking_wrapper(*args, **kwargs):
        __tracebackhide__ = True
        raise RuntimeError(_BLOCKED_MESSAGE)

    def _save(self):
        self._history.append(self._wrapper_class.ensure_connection)

    def unblock(self):
        self._save()
        self._wrapper_class.ensure_connection = self._real_ensure_connection
        return _BlockerContext(self)

    def block(self):
        self._save()
        self._wrapper_class.ensure_connection = self._blocking_wrapper
        return _BlockerContext(self)

    def restore(self):
        self._wrapper_class.ensure_connection = self._history.pop()


class _BlockerContext:
    def __init__(self, blocker):
        self._blocker = blocker

    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc_value, traceback):
        self._blocker.restore()


_blocker_key = pytest.StashKey()
# On Item.stash: the async connections the adb/atransactional_db fixture
# opened for this test.
_connections_key = pytest.StashKey()


def _marker_options(node):
    marker = node.get_closest_marker("async_db")
    if marker is None:
        return None
    transaction = marker.kwargs.get("transaction", False)
    databases = marker.kwargs.get("databases", None)
    if marker.args:
        transaction = marker.args[0]
    return {"transaction": bool(transaction), "databases": databases}


# ------------------------------------------------------------------ hooks


def pytest_addoption(parser):
    parser.addini(
        "django_async_backend_selector_loop",
        type="bool",
        default=True,
        help="On Windows, use the selector event loop policy (psycopg "
        "cannot run on the proactor loop).",
    )


def _uses_async_db(item):
    """Does this item get the database through this plugin?"""
    if _marker_options(item) is not None:
        return True
    fixturenames = getattr(item, "fixturenames", ())
    if "adb" in fixturenames or "atransactional_db" in fixturenames:
        return True
    cls = getattr(item, "cls", None)
    if cls is not None:
        from django_async_backend.test import AsyncioTransactionTestCase

        return issubclass(cls, AsyncioTransactionTestCase)
    return False


def _teach_pytest_django_our_fixtures():
    """pytest-django creates test databases only for the aliases its own
    ``db``/``transactional_db`` fixtures and ``django_db`` mark request. Make
    it count tests that use ``adb``, ``atransactional_db``, the ``async_db``
    mark or an ``AsyncioTestCase`` class as well; otherwise the async
    connections would hit the real database.
    """
    try:
        from pytest_django import fixtures as pd_fixtures
    except ImportError:  # pragma: no cover
        return
    original = getattr(pd_fixtures, "_get_databases_for_test", None)
    if original is None or getattr(original, "_django_async_backend", False):
        return

    def _get_databases_for_test(test):
        databases, serialized = original(test)
        if databases or not _uses_async_db(test):
            return databases, serialized
        from django_async_backend.test import AsyncioTransactionTestCase

        options = _marker_options(test) or {}
        wanted = options.get("databases")
        cls = getattr(test, "cls", None)
        if (
            wanted is None
            and cls is not None
            and issubclass(cls, AsyncioTransactionTestCase)
        ):
            return cls._database_aliases(), False
        aliases = AsyncioTransactionTestCase._database_aliases()
        if wanted is not None:
            aliases = [alias for alias in aliases if alias in wanted]
        return aliases, False

    _get_databases_for_test._django_async_backend = True
    pd_fixtures._get_databases_for_test = _get_databases_for_test


def pytest_configure(config):
    _teach_pytest_django_our_fixtures()
    config.addinivalue_line(
        "markers",
        "async_db(transaction=False, databases=None): give the test async "
        "database access through the adb (or atransactional_db) fixture.",
    )
    blocker = AsyncDbBlocker()
    config.stash[_blocker_key] = blocker
    blocker.block()

    if sys.platform == "win32" and config.getini(
        "django_async_backend_selector_loop"
    ):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            policy = asyncio.get_event_loop_policy()
            if not isinstance(policy, asyncio.WindowsSelectorEventLoopPolicy):
                asyncio.set_event_loop_policy(
                    asyncio.WindowsSelectorEventLoopPolicy()
                )


def pytest_unconfigure(config):
    blocker = config.stash.get(_blocker_key, None)
    if blocker is not None and blocker._history:
        blocker.restore()


def _release(node):
    """Every fixture and the test itself run as different asyncio tasks;
    whoever uses a connection next must be able to claim it."""
    for connection in node.stash.get(_connections_key, ()):
        connection._task = None


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    result = yield
    _release(request.node)
    return result


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    _release(item)
    return (yield)


def pytest_collection_modifyitems(items):
    for item in items:
        options = _marker_options(item)
        if options is None:
            continue
        name = "atransactional_db" if options["transaction"] else "adb"
        fixturenames = getattr(item, "fixturenames", None)
        if fixturenames is not None and name not in fixturenames:
            fixturenames.append(name)


# --------------------------------------------------------------- helpers


def _is_async_unittest(request):
    cls = getattr(request, "cls", None)
    if cls is None:
        return False
    from django_async_backend.test import AsyncioTransactionTestCase

    return issubclass(cls, AsyncioTransactionTestCase)


def _aliases(request):
    from django_async_backend.test import AsyncioTransactionTestCase

    options = _marker_options(request.node) or {}
    databases = options.get("databases")
    if databases is None:
        return AsyncioTransactionTestCase._database_aliases()
    return [
        alias
        for alias in AsyncioTransactionTestCase._database_aliases()
        if alias in databases
    ]


def _own_connections():
    from django_async_backend.db import async_connections

    task = asyncio.current_task()
    for connection in async_connections.all():
        connection._task = task


# -------------------------------------------------------------- fixtures


@pytest.fixture(scope="session")
def async_db_blocker(request):
    return request.config.stash[_blocker_key]


@pytest.fixture(autouse=True, scope="class")
def _async_db_unittest(request):
    """Unittest-style ``AsyncioTestCase`` classes manage their own
    connections and transactions; give them the test database and lift both
    blockers for the duration of the class.
    """
    if not _is_async_unittest(request):
        yield
        return
    request.getfixturevalue("django_db_setup")
    django_db_blocker = request.getfixturevalue("django_db_blocker")
    async_db_blocker = request.getfixturevalue("async_db_blocker")
    with django_db_blocker.unblock(), async_db_blocker.unblock():
        yield


@afixture
async def adb(request, django_db_setup, async_db_blocker):
    """A transaction per test on the async connections, rolled back after."""
    from django_async_backend.db import async_connections
    from django_async_backend.db.transaction import async_atomic
    from django_async_backend.test import (
        _acheck_constraints,
        release_task_ownership,
    )

    aliases = _aliases(request)
    with async_db_blocker.unblock():
        _own_connections()
        request.node.stash[_connections_key] = list(async_connections.all())
        atomics = {}
        for alias in aliases:
            atomic = async_atomic(alias)
            atomic._from_testcase = True
            await atomic.__aenter__()
            atomics[alias] = atomic
        # The test body runs as another task: let it claim the connections.
        release_task_ownership()
        try:
            yield
        finally:
            _own_connections()
            try:
                for alias in reversed(aliases):
                    await _acheck_constraints(async_connections[alias])
            finally:
                for alias in reversed(aliases):
                    connection = async_connections[alias]
                    connection.set_rollback(True)
                    await atomics[alias].__aexit__(None, None, None)
                for connection in async_connections.all():
                    await connection.close()


@afixture
async def atransactional_db(
    request, django_db_setup, async_db_blocker, django_db_blocker
):
    """Autocommit async connections; tables flushed after the test."""
    from asgiref.sync import sync_to_async
    from django.core.management import call_command
    from django.db import connections

    from django_async_backend.db import async_connections
    from django_async_backend.test import (
        AsyncioTransactionTestCase,
        release_task_ownership,
    )

    aliases = _aliases(request)
    flush_aliases = [
        alias
        for alias in AsyncioTransactionTestCase._fixture_aliases()
        if alias in aliases
    ]

    def flush():
        with django_db_blocker.unblock():
            for alias in reversed(flush_aliases):
                call_command(
                    "flush",
                    verbosity=0,
                    interactive=False,
                    database=alias,
                    reset_sequences=False,
                    allow_cascade=False,
                    inhibit_post_migrate=False,
                )
            for connection in connections.all(initialized_only=True):
                connection.close()

    with async_db_blocker.unblock():
        request.node.stash[_connections_key] = list(async_connections.all())
        release_task_ownership()
        try:
            yield
        finally:
            for connection in async_connections.all():
                await connection.close()
            await sync_to_async(flush)()


@pytest.fixture
def aclient():
    from django.test import AsyncClient

    return AsyncClient()


@afixture
async def aadmin_user(adb):
    """A superuser (``admin`` / ``password``) created with the async ORM."""
    from django.contrib.auth import get_user_model
    from django.contrib.auth.hashers import make_password

    UserModel = get_user_model()
    username_field = UserModel.USERNAME_FIELD
    username = "admin@example.com" if username_field == "email" else "admin"
    user = await UserModel.async_objects.filter(
        **{username_field: username}
    ).afirst()
    if user is None:
        data = {
            username_field: username,
            "password": make_password("password"),
            "is_staff": True,
            "is_superuser": True,
            "is_active": True,
        }
        if "email" in UserModel.REQUIRED_FIELDS:
            data["email"] = "admin@example.com"
        user = await UserModel.async_objects.acreate(**data)
    return user


@afixture
async def aadmin_client(aadmin_user):
    from django.test import AsyncClient

    client = AsyncClient()
    await client.aforce_login(aadmin_user)
    return client


class _AssertNumQueries:
    def __init__(self, config, exact):
        self.config = config
        self.exact = exact

    def __call__(self, num, connection=None, info=None, *, using=None):
        if connection is not None and using is not None:
            raise ValueError(
                'The "connection" and "using" parameter cannot be used '
                "together"
            )
        from django.db import DEFAULT_DB_ALIAS

        from django_async_backend.db import async_connections

        if connection is None:
            connection = async_connections[using or DEFAULT_DB_ALIAS]
        return self._context(num, connection, info)

    @contextlib.asynccontextmanager
    async def _context(self, num, connection, info):
        from django_async_backend.test import AsyncCaptureQueriesContext

        verbose = self.config.getoption("verbose") > 0
        async with AsyncCaptureQueriesContext(connection) as context:
            yield context
        performed = len(context)
        failed = num != performed if self.exact else performed > num
        if failed:
            msg = f"Expected to perform {num} queries "
            if not self.exact:
                msg += "or less "
            verb = "was" if performed == 1 else "were"
            msg += f"but {performed} {verb} done"
            if info:
                msg += f"\n{info}"
            if verbose:
                sqls = (q["sql"] for q in context.captured_queries)
                msg += "\n\nQueries:\n========\n\n" + "\n\n".join(sqls)
            else:
                msg += " (add -v option to show queries)"
            pytest.fail(msg)


@pytest.fixture
def adjango_assert_num_queries(pytestconfig):
    return _AssertNumQueries(pytestconfig, exact=True)


@pytest.fixture
def adjango_assert_max_num_queries(pytestconfig):
    return _AssertNumQueries(pytestconfig, exact=False)


@pytest.fixture
def adjango_capture_on_commit_callbacks():
    from django_async_backend.test import AsyncioTransactionTestCase

    return AsyncioTransactionTestCase.captureOnCommitCallbacks
