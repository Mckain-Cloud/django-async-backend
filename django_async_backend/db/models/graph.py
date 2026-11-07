"""Save an unsaved object graph on the async connection, dependencies first.

Test-data builders (factory_boy, model_bakery, hand-written helpers) tend to
build a whole graph of unsaved instances and save it in one go. Django's
``save()`` refuses an instance whose cached related object is unsaved, so the
graph has to be walked from the leaves inwards. :func:`asave_graph` does that
with ``async_save()``.
"""

from django.db import DEFAULT_DB_ALIAS

CONTENT_TYPE_LABEL = "contenttypes.ContentType"


def _is_content_type(obj):
    return obj._meta.label == CONTENT_TYPE_LABEL


async def _aresolve_content_type(ct, using):
    """An unsaved ``ContentType(app_label=..., model=...)`` stands in for the
    real row (model_bakery builds those in prepare mode). Look the row up
    instead of inserting a duplicate.
    """
    manager = type(ct).async_objects
    if using:
        manager = manager.using(using)
    return await manager.aget(app_label=ct.app_label, model=ct.model)


async def asave_graph(obj, *, using=None, _seen=None):
    """Save ``obj`` and every unsaved instance reachable through its cached
    forward relations (foreign keys, one-to-ones, generic foreign keys),
    dependencies first. Already-saved instances are left alone. Returns
    ``obj``.
    """
    seen = {} if _seen is None else _seen
    if id(obj) in seen:
        return obj
    seen[id(obj)] = obj

    opts = obj._meta
    for field in opts.concrete_fields:
        if not (field.many_to_one or field.one_to_one):
            continue
        if field.remote_field.parent_link:
            continue  # multi-table parents are saved by save() itself
        if not field.is_cached(obj):
            continue
        rel = field.get_cached_value(obj, default=None)
        if rel is None or not rel._state.adding:
            continue
        if _is_content_type(rel):
            rel = await _aresolve_content_type(rel, using)
            setattr(obj, field.name, rel)
        else:
            await asave_graph(rel, using=using, _seen=seen)
            # Django fills the attname at save time, but only when it is
            # still empty; refresh it explicitly so a stale value cannot win.
            # Assigning the attname drops the cached instance, so put it
            # back: callers expect ``obj.author`` to stay the object they
            # built, without a query.
            setattr(obj, field.attname, rel.pk)
            field.set_cached_value(obj, rel)

    for field in opts.private_fields:
        if type(field).__name__ != "GenericForeignKey":
            continue
        if not field.is_cached(obj):
            continue
        rel = field.get_cached_value(obj, default=None)
        if rel is None:
            continue
        if rel._state.adding:
            await asave_graph(rel, using=using, _seen=seen)
        if getattr(obj, field.fk_field) in (None, ""):
            setattr(obj, field.fk_field, rel.pk)

    if obj._state.adding:
        if using and using != DEFAULT_DB_ALIAS:
            await obj.async_save(using=using)
        else:
            await obj.async_save()
    return obj
