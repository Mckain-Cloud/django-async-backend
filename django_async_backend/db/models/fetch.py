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

from django.core.exceptions import SynchronousOnlyOperation
from django.db.models.fields.related_descriptors import (
    ForwardManyToOneDescriptor,
    ReverseOneToOneDescriptor,
)
from django.db.models.query_utils import DeferredAttribute


def forward_relation_names(model):
    """Names of the model's forward FK / one-to-one fields (select_related-able)."""
    return [
        f.name
        for f in model._meta.concrete_fields
        if f.is_relation and (f.many_to_one or f.one_to_one)
    ]


def _uncached_forward_relations(obj):
    return [
        f.name
        for f in obj._meta.concrete_fields
        if f.is_relation
        and (f.many_to_one or f.one_to_one)
        and not f.is_cached(obj)
    ]


async def aprepare_str(*objs, depth=3):
    """Make ``str(obj)`` safe to call from async code for each of ``objs``.

    A ``__str__`` that reads a forward relation (``Permission`` reads
    ``content_type``) lazy-loads it through the sync ORM, which raises
    ``SynchronousOnlyOperation`` in a task. Try ``str()``; when it trips,
    ``afetch()`` the instance's unfetched forward relations and try again,
    following the relations it just loaded up to ``depth`` levels (a label
    that reads ``obj.a.b``). A model whose label needs no relation costs one
    ``str()`` call and no query.
    """
    for obj in objs:
        if obj is None:
            continue
        frontier = [obj]
        for _ in range(depth):
            try:
                str(obj)
                break
            except SynchronousOnlyOperation:
                next_frontier = []
                for o in frontier:
                    names = _uncached_forward_relations(o)
                    if not names:
                        continue
                    loaded = await o.afetch(*names)
                    if len(names) == 1:
                        loaded = [loaded]
                    next_frontier.extend(v for v in loaded if v is not None)
                if not next_frontier:
                    raise
                frontier = next_frontier
        else:
            str(obj)  # still failing: let the error through


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
    value = (
        await manager.filter(pk=instance.pk)
        .values_list(field.attname, flat=True)
        .aget()
    )
    setattr(instance, field.attname, value)
    if field.is_relation and field.is_cached(instance):
        field.delete_cached_value(instance)
    return value


async def _fetch_generic_foreign_key(descriptor, instance):
    from django_async_backend.utils.contenttypes import aget_for_id

    field = descriptor.field
    ct_attname = field.model._meta.get_field(field.ct_field).attname
    ct_id = getattr(instance, ct_attname, None)
    pk_val = getattr(instance, field.fk_field)
    rel_obj = None
    if ct_id is not None and pk_val is not None:
        ct = await aget_for_id(ct_id, using=instance._state.db)
        model = ct.model_class()
        if model is not None:
            rel_obj = (
                await model.async_objects.using(instance._state.db)
                .filter(pk=pk_val)
                .afirst()
            )
            if rel_obj is not None:
                rel_obj._state.fetch_mode = instance._state.fetch_mode
    field.set_cached_value(instance, rel_obj)
    return rel_obj


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
        elif type(descriptor).__name__ == "GenericForeignKeyDescriptor":
            values.append(await _fetch_generic_foreign_key(descriptor, self))
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
