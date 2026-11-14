import asyncio
import contextvars
import inspect
from collections import Counter
from functools import wraps
from unittest import IsolatedAsyncioTestCase

from django.core.management import call_command
from django.core.signals import request_started
from django.db import (
    DEFAULT_DB_ALIAS,
    connections,
    reset_queries,
)
from django.db.utils import load_backend
from django.test.testcases import TestData
from django.test.utils import modify_settings as _modify_settings
from django.test.utils import override_settings as _override_settings

from django_async_backend.core.fixtures import aload_fixtures
from django_async_backend.db import async_connections
from django_async_backend.db.transaction import async_atomic

__all__ = [
    "AsyncioTestCase",
    "AsyncioTransactionTestCase",
    "AsyncCaptureQueriesContext",
    "aload_fixtures",
    "modify_settings",
    "override_settings",
    "release_task_ownership",
]


def _refresh_connection_task_ownership_decorator(fn):
    @wraps(fn)
    async def inner(*args, **kwargs):
        task = asyncio.current_task()
        for connection in async_connections.all():
            connection._task = task
        try:
            return await fn(*args, **kwargs)
        finally:
            # The next phase runs as another task. Releasing ownership before
            # this one finishes also tells the orphan reaper (db/orphans.py)
            # that the connections are being handed on, not abandoned.
            release_task_ownership()

    inner._refreshes_task_ownership = True
    return inner


def release_task_ownership():
    """Forget which task owns the async connections, so the next task that
    uses one claims it. Test harnesses whose setup, test body and teardown run
    as different tasks (pytest-asyncio fixtures, for example) call this at the
    end of each phase.
    """
    for connection in async_connections.all():
        connection._task = None


async def _acheck_constraints(connection):
    """Like Django's test teardown: surface deferred constraint violations
    before the transaction is rolled back.
    """
    if connection.connection is None or connection.needs_rollback:
        return
    if not connection.features.can_defer_constraint_checks:
        return
    async with await connection.cursor() as cursor:
        await cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        await cursor.execute("SET CONSTRAINTS ALL DEFERRED")


class AsyncioTransactionTestCase(IsolatedAsyncioTestCase):
    """Each test runs on its own event loop with autocommit connections. The
    tables are flushed after every test, like Django's TransactionTestCase.
    """

    databases = "__all__"
    fixtures = None

    _overridden_settings = None
    _modified_settings = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if cls._overridden_settings:
            cls.enterClassContext(
                _override_settings(**cls._overridden_settings)
            )
        if cls._modified_settings:
            cls.enterClassContext(_modify_settings(cls._modified_settings))

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

        for name in dir(cls):
            if not name.startswith("test"):
                continue

            method = getattr(cls, name, None)
            if not callable(method) or getattr(
                method, "_refreshes_task_ownership", False
            ):
                continue

            setattr(
                cls,
                name,
                _refresh_connection_task_ownership_decorator(method),
            )

    # ----------------------------------------------------------- databases

    @classmethod
    def _database_aliases(cls):
        """Async connection aliases this class uses, in settings order.
        Works outside the event loop (no wrapper is created).
        """
        aliases = [
            alias
            for alias in async_connections
            if hasattr(
                load_backend(async_connections.settings[alias]["ENGINE"]),
                "AsyncDatabaseWrapper",
            )
        ]
        if cls.databases == "__all__":
            return aliases
        return [alias for alias in aliases if alias in cls.databases]

    @classmethod
    def _fixture_aliases(cls):
        """Aliases that back distinct databases: fixtures are loaded and
        flushes run once per database, not once per alias.
        """
        seen = set()
        result = []
        for alias in cls._database_aliases():
            signature = connections[alias].creation.test_db_signature()
            if signature in seen:
                continue
            seen.add(signature)
            result.append(alias)
        return result

    @classmethod
    async def _aload_fixtures(cls):
        if not cls.fixtures:
            return
        for alias in cls._fixture_aliases():
            await aload_fixtures(cls.fixtures, using=alias)

    def _flush_databases(self):
        # Runs outside the event loop, on the sync connections.
        for alias in reversed(self._fixture_aliases()):
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

    # ------------------------------------------------------------ settings

    def settings(self, **kwargs):
        return override_settings(**kwargs)

    def modify_settings(self, **kwargs):
        return modify_settings(**kwargs)

    # ----------------------------------------------------------- lifecycle

    def _callSetUp(self):
        self._asyncioRunner.get_loop()
        self._asyncioTestContext.run(self.setUp)
        self._callAsync(
            _refresh_connection_task_ownership_decorator(self._aload_fixtures)
        )
        self._callAsync(
            _refresh_connection_task_ownership_decorator(self.asyncSetUp)
        )

    async def _close_connection(self):
        for connection in async_connections.all():
            await connection.close()

    def _callTearDown(self):
        self._callAsync(
            _refresh_connection_task_ownership_decorator(self.asyncTearDown)
        )
        self._callAsync(
            _refresh_connection_task_ownership_decorator(
                self._close_connection
            )
        )
        self._asyncioTestContext.run(self.tearDown)
        self._flush_databases()

    # ---------------------------------------------------------- assertions

    def assertNumQueries(
        self, num, func=None, *args, using=DEFAULT_DB_ALIAS, **kwargs
    ):
        """``async with self.assertNumQueries(n):`` or
        ``await self.assertNumQueries(n, coroutine_function, *args)``.
        """
        context = _AsyncAssertNumQueriesContext(
            self, num, async_connections[using]
        )
        if func is None:
            return context

        async def run():
            async with context:
                result = func(*args, **kwargs)
                if inspect.isawaitable(result):
                    await result

        return run()

    async def assertQuerySetEqual(
        self, qs, values, transform=None, ordered=True, msg=None
    ):
        values = list(values)
        if hasattr(qs, "__aiter__"):
            items = [item async for item in qs]
        else:
            items = list(qs)
        if transform is not None:
            items = [transform(item) for item in items]
        if not ordered:
            return self.assertDictEqual(
                Counter(items), Counter(values), msg=msg
            )
        # For example qs.iterator() could be passed as qs, but it does not
        # have 'ordered' attribute.
        if len(values) > 1 and hasattr(qs, "ordered") and not qs.ordered:
            raise ValueError(
                "Trying to compare non-ordered queryset against more than one "
                "ordered value."
            )
        return self.assertEqual(items, values, msg=msg)

    @classmethod
    def captureOnCommitCallbacks(
        cls, *, using=DEFAULT_DB_ALIAS, execute=False
    ):
        """Async context manager that captures ``on_commit()`` callbacks
        registered on the async connection ``using``. With ``execute=True``
        the callbacks run (and are awaited) when the block ends.
        """
        return _AsyncCaptureOnCommitCallbacks(using, execute)


class AsyncioTestCase(AsyncioTransactionTestCase):
    """Like Django's TestCase: one event loop and one database transaction
    per class, with ``fixtures`` and ``asetUpTestData()`` loaded once inside
    it, and a savepoint per test that is rolled back afterwards.
    """

    _class_runner = None
    _class_context = None
    _class_atomics = None

    # ---------------------------------------------------------- class setup

    @classmethod
    def _runner_kwargs(cls):
        kwargs = {"debug": True}
        loop_factory = getattr(cls, "loop_factory", None)
        if loop_factory is not None:  # Python 3.13+
            kwargs["loop_factory"] = loop_factory
        return kwargs

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        runner = asyncio.Runner(**cls._runner_kwargs())
        cls._class_runner = runner
        # Every coroutine of this class runs in this one Context object, so
        # the connections (which asgiref keeps in a context variable) opened
        # here are the ones the tests see.
        cls._class_context = contextvars.copy_context()
        run = cls._run_in_class_context
        try:
            run(cls._aenter_class_atomics())
            try:
                run(
                    _refresh_connection_task_ownership_decorator(
                        cls._aload_fixtures
                    )()
                )
                pre_attrs = cls.__dict__.copy()
                run(
                    _refresh_connection_task_ownership_decorator(
                        cls.asetUpTestData
                    )()
                )
                for name, value in cls.__dict__.items():
                    if value is not pre_attrs.get(name):
                        setattr(cls, name, TestData(name, value))
            except BaseException:
                run(cls._aexit_class_atomics())
                raise
        except BaseException:
            cls._class_runner = cls._class_context = None
            runner.close()
            raise

    @classmethod
    def _run_in_class_context(cls, coro):
        return cls._class_runner.run(coro, context=cls._class_context)

    @classmethod
    def tearDownClass(cls):
        runner = cls._class_runner
        try:
            if runner is not None:
                cls._run_in_class_context(cls._aexit_class_atomics())
        finally:
            cls._class_runner = cls._class_context = None
            if runner is not None:
                runner.close()
            super().tearDownClass()

    @classmethod
    @_refresh_connection_task_ownership_decorator
    async def _aenter_class_atomics(cls):
        cls._class_atomics = {}
        for alias in cls._database_aliases():
            atomic = async_atomic(alias)
            atomic._from_testcase = True
            await atomic.__aenter__()
            cls._class_atomics[alias] = atomic

    @classmethod
    @_refresh_connection_task_ownership_decorator
    async def _aexit_class_atomics(cls):
        atomics = cls._class_atomics or {}
        try:
            for alias in reversed(list(atomics)):
                async_connections[alias].set_rollback(True)
                await atomics[alias].__aexit__(None, None, None)
        finally:
            cls._class_atomics = None
            for connection in async_connections.all():
                await connection.close()

    @classmethod
    async def asetUpTestData(cls):
        """Load initial data for the whole class, inside its transaction."""

    # The class owns the runner (and so the event loop) because the
    # connection that holds the class transaction must stay on one loop.
    def _setupAsyncioRunner(self):
        runner = type(self)._class_runner
        if runner is None:
            return super()._setupAsyncioRunner()
        assert self._asyncioRunner is None
        self._asyncioRunner = runner
        self._asyncioTestContext = type(self)._class_context

    def _tearDownAsyncioRunner(self):
        if self._asyncioRunner is type(self)._class_runner:
            self._asyncioRunner = None
            return
        super()._tearDownAsyncioRunner()

    # ----------------------------------------------------------- per test

    @_refresh_connection_task_ownership_decorator
    async def _init_transaction(self):
        self.connections = {}
        self.atomic_cms = {}
        self.atomics = {}

        for alias in self._database_aliases():
            connection = async_connections[alias]
            self.connections[alias] = connection
            atomic = async_atomic(alias)
            atomic._from_testcase = True
            self.atomic_cms[alias] = atomic
            self.atomics[alias] = await atomic.__aenter__()
            # The connection outlives the test; start its query log afresh.
            connection.queries_log.clear()

    async def _close_transaction(self):
        try:
            for alias in reversed(list(self.atomic_cms)):
                await _acheck_constraints(self.connections[alias])
        finally:
            for alias in reversed(list(self.atomic_cms)):
                connection = self.connections[alias]
                connection.set_rollback(True)
                await self.atomic_cms[alias].__aexit__(None, None, None)
            if type(self)._class_runner is None:
                # No class transaction (setUpClass was bypassed): behave
                # like before and drop the connection.
                for connection in self.connections.values():
                    await connection.close()

    def _callSetUp(self):
        # Force loop to be initialized and set as the current loop
        # so that setUp functions can use get_event_loop() and get the
        # correct loop instance.
        self._asyncioRunner.get_loop()
        self._asyncioTestContext.run(self.setUp)
        self._callAsync(self._init_transaction)
        self._callAsync(
            _refresh_connection_task_ownership_decorator(self.asyncSetUp)
        )

    def _callTearDown(self):
        self._callAsync(
            _refresh_connection_task_ownership_decorator(self.asyncTearDown)
        )
        self._callAsync(
            _refresh_connection_task_ownership_decorator(
                self._close_transaction
            )
        )
        self._asyncioTestContext.run(self.tearDown)

    def _flush_databases(self):
        # Everything is rolled back; nothing to flush.
        pass


def _save_options_for_supported_class(decorator, cls):
    from django.test import SimpleTestCase

    if not issubclass(cls, (SimpleTestCase, AsyncioTransactionTestCase)):
        raise ValueError(
            "Only subclasses of Django SimpleTestCase or "
            "AsyncioTransactionTestCase can be decorated with %s"
            % type(decorator).__name__
        )
    decorator.save_options(cls)
    return cls


class override_settings(_override_settings):
    def decorate_class(self, cls):
        return _save_options_for_supported_class(self, cls)


class modify_settings(_modify_settings):
    def decorate_class(self, cls):
        return _save_options_for_supported_class(self, cls)


class AsyncCaptureQueriesContext:

    def __init__(self, connection):
        self.connection = connection

    def __len__(self):
        return len(self.captured_queries)

    @property
    def captured_queries(self):
        return self.connection.queries[
            slice(self.initial_queries, self.final_queries)
        ]

    async def __aenter__(self):
        self.force_debug_cursor = self.connection.force_debug_cursor
        self.connection.force_debug_cursor = True
        # Run any initialization queries if needed so that they won't be
        # included as part of the count.
        await self.connection.ensure_connection()
        self.initial_queries = len(self.connection.queries_log)
        self.final_queries = None
        self.reset_queries_disconnected = request_started.disconnect(
            reset_queries
        )
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        self.connection.force_debug_cursor = self.force_debug_cursor
        if self.reset_queries_disconnected:
            request_started.connect(reset_queries)
        if exc_type is not None:
            return
        self.final_queries = len(self.connection.queries_log)


class _AsyncAssertNumQueriesContext(AsyncCaptureQueriesContext):
    def __init__(self, test_case, num, connection):
        self.test_case = test_case
        self.num = num
        super().__init__(connection)

    async def __aexit__(self, exc_type, exc_value, traceback):
        await super().__aexit__(exc_type, exc_value, traceback)
        if exc_type is not None:
            return
        executed = len(self)
        self.test_case.assertEqual(
            executed,
            self.num,
            "%d queries executed, %d expected\nCaptured queries were:\n%s"
            % (
                executed,
                self.num,
                "\n".join(
                    "%d. %s" % (i, query["sql"])
                    for i, query in enumerate(self.captured_queries, start=1)
                ),
            ),
        )


class _AsyncCaptureOnCommitCallbacks:
    def __init__(self, using, execute):
        self.connection = async_connections[using]
        self.execute = execute
        self.callbacks = []

    async def __aenter__(self):
        self.start_count = len(self.connection.run_on_commit)
        return self.callbacks

    async def __aexit__(self, exc_type, exc_value, traceback):
        start_count = self.start_count
        while True:
            callback_count = len(self.connection.run_on_commit)
            for _, callback, robust in self.connection.run_on_commit[
                start_count:
            ]:
                self.callbacks.append(callback)
                if self.execute:
                    await self._run(callback, robust)
            if callback_count == len(self.connection.run_on_commit):
                break
            start_count = callback_count

    @staticmethod
    async def _run(callback, robust):
        try:
            result = callback()
            if inspect.isawaitable(result):
                await result
        except Exception:
            if not robust:
                raise
