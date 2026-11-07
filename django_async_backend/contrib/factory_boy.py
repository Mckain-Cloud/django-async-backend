"""factory_boy on the async connection.

``factory.django.DjangoModelFactory`` builds the whole object graph without
the database and only saves at the end, so an async variant is build-then-save:
``acreate()`` runs the ordinary ``build()`` and then saves the graph with
``async_save()``, dependencies first.

Usage::

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

What works: sequences, ``LazyAttribute``, ``SubFactory`` (saved first),
``RelatedFactory``/``RelatedFactoryList`` (saved after the instance),
``django_get_or_create`` (looked up with the async manager),
``Meta.database``, ``Trait``, sync ``post_generation`` hooks that do not
touch the database, ``async_post_generation`` hooks that do, and
``AsyncIterator`` for ``Iterator``-style choices drawn from a queryset.

What does not: a sync ``post_generation`` hook or ``PostGenerationMethodCall``
that queries the database (it raises ``SynchronousOnlyOperation``; make it an
``async_post_generation`` hook) and plain ``factory.Iterator`` over a queryset
(use ``AsyncIterator``).
"""

import contextvars
import itertools

from django.db import DEFAULT_DB_ALIAS
from django.db.models import Model
from factory import utils
from factory.base import BaseFactory
from factory.declarations import (
    BaseDeclaration,
    Iterator,
    PostGenerationDeclaration,
    RelatedFactory,
    SubFactory,
)
from factory.django import DjangoModelFactory

from django_async_backend.db.models.convert import (
    is_sync_queryset,
    to_async_queryset,
)
from django_async_backend.db.models.graph import asave_graph

__all__ = [
    "AsyncDjangoModelFactory",
    "AsyncIterator",
    "AsyncPostGeneration",
    "async_post_generation",
]

# The list of deferred work collected while ``acreate()``/``abuild()`` run
# the synchronous build: ("built", factory, instance) for every instance an
# AsyncDjangoModelFactory produced (sub-factories first), ("hook",
# declaration, instance, context) and ("related", instance).
_pending = contextvars.ContextVar("django_async_backend.factory_boy.pending")


class AsyncPostGeneration(PostGenerationDeclaration):
    """``post_generation`` whose function is a coroutine function. It runs
    (awaited) after the instance and its relations are saved::

        @async_post_generation
        async def groups(self, create, extracted, **kwargs):
            ...
    """

    def __init__(self, function):
        super().__init__()
        self.function = function

    def call(self, instance, step, context):
        pending = _pending.get(None)
        if pending is None:
            raise RuntimeError(
                "%s.%s is an async_post_generation hook, which only runs "
                "through AsyncDjangoModelFactory.acreate()/abuild()."
                % (self.function.__module__, self.function.__name__)
            )
        pending.append(("hook", self, instance, context))
        return None


def async_post_generation(function):
    return AsyncPostGeneration(function)


class AsyncIterator(Iterator):
    """``factory.Iterator`` over a queryset, evaluated on the async
    connection. Pass the queryset or a callable returning it; ``acreate()``
    and ``abuild()`` prime it before building, or call ``await
    Factory.aprime()`` yourself.
    """

    def __init__(self, queryset, cycle=True, getter=None):
        BaseDeclaration.__init__(self)
        self.getter = getter
        self.iterator = None
        self.cycle = cycle
        self.queryset = queryset
        self._primed = False
        self.iterator_builder = self._not_primed

    def _not_primed(self):
        raise RuntimeError(
            "AsyncIterator has not been primed. Use acreate()/abuild(), or "
            "await Factory.aprime() before build()."
        )

    async def aprime(self, force=False):
        if self._primed and not force:
            return
        qs = self.queryset
        if callable(qs) and not hasattr(qs, "__aiter__"):
            qs = qs()
        if is_sync_queryset(qs):
            qs = to_async_queryset(qs)
        values = [value async for value in qs]
        if self.cycle:
            self.iterator_builder = lambda: utils.ResetableIterator(
                itertools.cycle(values)
            )
        else:
            self.iterator_builder = lambda: utils.ResetableIterator(
                iter(values)
            )
        self.iterator = None
        self._primed = True


class AsyncDjangoModelFactory(DjangoModelFactory):
    class Meta:
        abstract = True

    # ----------------------------------------------------------- priming

    @classmethod
    def _declarations(cls, seen=None):
        """Yield every declaration of this factory and of the factories it
        reaches through SubFactory/RelatedFactory.
        """
        seen = set() if seen is None else seen
        if cls in seen:
            return
        seen.add(cls)
        for declaration_set in (
            cls._meta.pre_declarations,
            cls._meta.post_declarations,
        ):
            for declaration in declaration_set.declarations.values():
                yield declaration
                if isinstance(declaration, (SubFactory, RelatedFactory)):
                    sub = declaration.get_factory()
                    if isinstance(sub, type) and issubclass(sub, BaseFactory):
                        if hasattr(sub, "_declarations"):
                            yield from sub._declarations(seen)

    @classmethod
    async def aprime(cls, force=False):
        """Evaluate every ``AsyncIterator`` this factory (or a nested one)
        declares.
        """
        for declaration in cls._declarations():
            if isinstance(declaration, AsyncIterator):
                await declaration.aprime(force=force)

    # -------------------------------------------------------- generation

    @classmethod
    def _after_postgeneration(cls, instance, create, results=None):
        pending = _pending.get(None)
        if pending is None:
            return
        if isinstance(instance, Model):
            pending.append(("built", cls, instance))
        post = cls._meta.post_declarations.declarations
        for name, result in (results or {}).items():
            if not isinstance(post.get(name), RelatedFactory):
                continue
            related = result if isinstance(result, (list, tuple)) else [result]
            for obj in related:
                if isinstance(obj, Model):
                    pending.append(("related", obj))

    @classmethod
    def _collect(cls, build, kwargs):
        pending = []
        token = _pending.set(pending)
        try:
            instance = build(**kwargs)
        finally:
            _pending.reset(token)
        return instance, pending

    @classmethod
    def _using(cls):
        database = cls._meta.database
        return None if database == DEFAULT_DB_ALIAS else database

    @classmethod
    async def _aget_or_create(cls, instance):
        """``django_get_or_create``: if a row matching the key fields exists,
        turn the built ``instance`` into that row (so whatever references
        it, such as a parent built by a SubFactory, now points at the saved
        object). Returns True when an existing row was adopted.
        """
        key_fields = cls._meta.django_get_or_create
        if not key_fields or not instance._state.adding:
            return False
        using = cls._using()
        lookup = {}
        for name in key_fields:
            value = getattr(instance, name)
            if isinstance(value, Model) and value._state.adding:
                await asave_graph(value, using=using)
            lookup[name] = value
        manager = type(instance).async_objects
        if using:
            manager = manager.using(using)
        existing = await manager.filter(**lookup).afirst()
        if existing is None:
            return False
        instance.__dict__.update(existing.__dict__)
        return True

    @classmethod
    async def _asave(cls, instance, pending):
        """Save ``instance`` and its dependencies, honouring
        ``django_get_or_create`` on every AsyncDjangoModelFactory that took
        part in the build (sub-factories first).
        """
        for item in pending:
            if item[0] == "built":
                await item[1]._aget_or_create(item[2])
        return await asave_graph(instance, using=cls._using())

    @classmethod
    async def _arun_pending(cls, pending, create):
        using = cls._using()
        if create:
            for item in pending:
                if item[0] == "related":
                    await asave_graph(item[1], using=using)
        for item in pending:
            if item[0] == "hook":
                _, declaration, instance, context = item
                await declaration.function(
                    instance, create, context.value, **context.extra
                )

    @classmethod
    async def acreate(cls, **kwargs):
        """Build the instance graph and save it on the async connection."""
        await cls.aprime()
        instance, pending = cls._collect(cls.build, kwargs)
        instance = await cls._asave(instance, pending)
        await cls._arun_pending(pending, create=True)
        return instance

    @classmethod
    async def acreate_batch(cls, size, **kwargs):
        return [await cls.acreate(**kwargs) for _ in range(size)]

    @classmethod
    async def abuild(cls, **kwargs):
        """``build()`` that also runs ``async_post_generation`` hooks (with
        ``create=False``) and primes ``AsyncIterator`` declarations.
        """
        await cls.aprime()
        instance, pending = cls._collect(cls.build, kwargs)
        await cls._arun_pending(pending, create=False)
        return instance

    @classmethod
    async def abuild_batch(cls, size, **kwargs):
        return [await cls.abuild(**kwargs) for _ in range(size)]
