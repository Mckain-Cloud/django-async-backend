"""``await instance.afetch(...)``: explicit async loading of lazy relations and
deferred fields.

Django loads a forward FK, a reverse one-to-one or a deferred field on
attribute access, through the *sync* ORM, which raises
``SynchronousOnlyOperation`` in async code. ``afetch()`` performs the same
load on the task's async connection and fills the same caches, so the plain
attribute works afterwards::

    book = await Book.async_objects.aget(pk=1)
    author = await book.afetch("author")      # one query
    assert book.author is author              # cached, no query

Multi-valued relations (reverse FK, M2M, generic relations) are loaded with
the async ``prefetch_related_objects()``, after which ``instance.<rel>.all()``
is served from the prefetch cache.

Custom descriptors can take part by defining
``async def aget_object(self, instance)``; whatever it returns is the value.
"""

from inspect import getattr_static

from django.db.models.fields.related_descriptors import (
    ForwardManyToOneDescriptor,
    ReverseOneToOneDescriptor,
)
from django.db.models.query_utils import DeferredAttribute


async def _fetch_forward(descriptor, instance):
    field = descriptor.field
    if None in field.get_local_related_value(instance):
        rel_obj = None
    else:
        from django_async_backend.db.models.prefetch import (
            _descriptor_queryset,
        )

        qs = _descriptor_queryset(field.remote_field.model, instance)
        rel_obj = await qs.aget(field.get_reverse_related_filter(instance))
        if not field.remote_field.multiple:
            field.remote_field.set_cached_value(rel_obj, instance)
    field.set_cached_value(instance, rel_obj)
    return rel_obj


async def _fetch_reverse_one_to_one(descriptor, instance):
    from django_async_backend.db.models.prefetch import _descriptor_queryset

    related = descriptor.related
    qs = _descriptor_queryset(related.related_model, instance)
    filter_args = related.field.get_forward_related_filter(instance)
    rel_obj = await qs.filter(**filter_args).afirst()
    if rel_obj is not None:
        related.field.set_cached_value(rel_obj, instance)
    related.set_cached_value(instance, rel_obj)
    return rel_obj


async def _fetch_deferred(descriptor, instance):
    field = descriptor.field
    manager = instance.__class__._async_base_manager.db_manager(
        instance._state.db, hints={"instance": instance}
    )
    value = await manager.filter(pk=instance.pk).values_list(
        field.attname, flat=True
    ).aget()
    setattr(instance, field.attname, value)
    if field.is_relation and field.is_cached(instance):
        field.delete_cached_value(instance)
    return value


async def _fetch_multi(name, instance):
    from django_async_backend.db.models.query import prefetch_related_objects

    await prefetch_related_objects([instance], name)
    return list(getattr(instance, name).all())


async def afetch(self, *names):
    """Load the named relations / deferred fields asynchronously.

    Returns the value for a single name, or a list of values in order when
    several names are given.
    """
    if not names:
        raise TypeError("afetch() needs at least one field name.")
    values = []
    for name in names:
        descriptor = getattr_static(type(self), name, None)
        if descriptor is None:
            raise AttributeError(
                f"{type(self).__name__} has no attribute {name!r}."
            )
        if hasattr(descriptor, "aget_object"):
            values.append(await descriptor.aget_object(self))
        elif isinstance(descriptor, ForwardManyToOneDescriptor):
            values.append(await _fetch_forward(descriptor, self))
        elif isinstance(descriptor, ReverseOneToOneDescriptor):
            values.append(await _fetch_reverse_one_to_one(descriptor, self))
        elif isinstance(descriptor, DeferredAttribute):
            values.append(await _fetch_deferred(descriptor, self))
        elif hasattr(descriptor, "related_manager_cls") or hasattr(
            descriptor, "rel"
        ):
            values.append(await _fetch_multi(name, self))
        else:
            raise TypeError(
                f"afetch() does not know how to load "
                f"{type(self).__name__}.{name} ({type(descriptor).__name__})."
            )
    return values[0] if len(names) == 1 else values
