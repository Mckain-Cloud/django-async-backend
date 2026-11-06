"""A custom descriptor opts into prefetch_related() and afetch() by
implementing the async hooks ``aget_prefetch_querysets`` / ``aget_object``.
"""

from django.db import DEFAULT_DB_ALIAS
from test_app.models import TestModel

from django_async_backend.db import async_connections
from django_async_backend.test import (
    AsyncCaptureQueriesContext,
    AsyncioTestCase,
)


class HighestRelative:
    """``obj.highest_relative``: the child (relative=obj) with the largest
    value. A single-valued, custom, non-field relation."""

    cache_name = "_highest_relative"

    def __get__(self, instance, cls=None):
        if instance is None:
            return self
        try:
            return instance.__dict__[self.cache_name]
        except KeyError:
            raise AttributeError("highest_relative is not loaded")

    def is_cached(self, instance):
        return self.cache_name in instance.__dict__

    def get_prefetch_querysets(self, instances, querysets=None):
        # Django's discovery looks for this name; the async backend never
        # calls it.
        raise NotImplementedError

    async def aget_prefetch_querysets(self, instances, querysets=None):
        qs = TestModel.async_objects.filter(relative__in=instances).order_by(
            "relative_id", "-value"
        )
        return (
            qs,
            lambda child: child.relative_id,
            lambda parent: parent.pk,
            True,  # single
            self.cache_name,
            True,  # is_descriptor: stored with setattr(obj, cache_name, val)
        )

    async def aget_object(self, instance):
        child = (
            await TestModel.async_objects.filter(relative=instance)
            .order_by("-value")
            .afirst()
        )
        instance.__dict__[self.cache_name] = child
        return child


class CustomDescriptorTests(AsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        TestModel.highest_relative = HighestRelative()

    @classmethod
    def tearDownClass(cls):
        del TestModel.highest_relative
        super().tearDownClass()

    async def asyncSetUp(self):
        self.p1 = await TestModel.async_objects.acreate(name="p1", value=0)
        self.p2 = await TestModel.async_objects.acreate(name="p2", value=0)
        for name, value, parent in (
            ("c1", 1, self.p1),
            ("c2", 9, self.p1),
            ("c3", 5, self.p2),
        ):
            await TestModel.async_objects.acreate(
                name=name, value=value, relative=parent
            )

    async def test_prefetch_related_uses_the_async_hook(self):
        async with AsyncCaptureQueriesContext(
            async_connections[DEFAULT_DB_ALIAS]
        ) as ctx:
            parents = [
                p
                async for p in TestModel.async_objects.filter(
                    relative__isnull=True
                )
                .order_by("pk")
                .prefetch_related("highest_relative")
            ]
            highest = [p.highest_relative.name for p in parents]

        self.assertEqual(highest, ["c2", "c3"])
        self.assertEqual(len(ctx), 2)

    async def test_afetch_uses_aget_object(self):
        parent = await TestModel.async_objects.aget(pk=self.p1.pk)
        child = await parent.afetch("highest_relative")
        self.assertEqual(child.name, "c2")
        self.assertIs(parent.highest_relative, child)
