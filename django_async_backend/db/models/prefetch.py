"""Async counterpart of ``django.db.models.query.prefetch_one_level()``.

Django resolves a prefetch by asking the descriptor (or the related manager)
for ``get_prefetch_querysets()``. Every implementation of that hook builds a
*sync* queryset and, for single-valued relations, iterates it right away to
populate the reverse relation cache. Both halves are unusable here: the
queryset has to be an async one, and it can only be consumed with ``async
for``.

So the descriptor code is mirrored below instead of being called. Each branch
returns the same six-tuple Django's hook returns -- unevaluated -- plus a
callback that performs the cache bookkeeping the sync code did while
iterating. ``prefetch_one_level()`` then evaluates the queryset with ``async
for`` and runs that callback.

Generic foreign keys resolve ContentTypes asynchronously and evaluate one
queryset per target model. Generic relations warm Django's ContentType cache
before constructing the related manager. Custom descriptors remain unsupported.
"""

import contextvars
import copy
from collections import defaultdict

from django.apps import apps
from django.core import exceptions
from django.db import NotSupportedError
from django.db.models import Q
from django.db.models.constants import LOOKUP_SEP
from django.db.models.expressions import ColPairs
from django.db.models.fields.related_descriptors import (
    ForwardManyToOneDescriptor,
    ReverseOneToOneDescriptor,
    _filter_prefetch_queryset,
)
from django.db.models.fields.tuple_lookups import TupleIn
from django.db.models.query import get_prefetcher as django_get_prefetcher

from django_async_backend.db.models.pipeline import (
    aevaluate_pipelined,
    pipeline_enabled,
)
from django_async_backend.utils.contenttypes import (
    aget_for_id,
    aget_for_model,
)

RELATED_DESCRIPTORS_MODULE = "django.db.models.fields.related_descriptors"
CONTENTTYPES_MODULE = "django.contrib.contenttypes.fields"


async def get_prefetcher(instance, through_attr, to_attr):
    """Django's discovery with async preparation for generic managers."""
    await _prepare_generic_relation(instance, through_attr)
    return django_get_prefetcher(instance, through_attr, to_attr)


async def _prepare_generic_relation(instance, through_attr):
    """Resolve ContentType before Django constructs a generic manager.

    Its synchronous constructor then uses the shared ContentType cache.
    Import lazily so prefetching doesn't require the contenttypes app.
    """
    if not apps.is_installed("django.contrib.contenttypes"):
        return
    from django.contrib.contenttypes.fields import (
        ReverseGenericManyToOneDescriptor,
    )

    descriptor = getattr(type(instance), through_attr, None)
    if isinstance(descriptor, ReverseGenericManyToOneDescriptor):
        await aget_for_model(
            type(instance),
            using=instance._state.db,
            for_concrete_model=descriptor.rel.field.for_concrete_model,
        )


def _async_queryset(model, using=None, hints=None):
    """Async equivalent of ``Manager.get_queryset()`` on a plain manager."""
    # Imported lazily: django_async_backend.db.models.query imports this
    # module at import time.
    from django_async_backend.db.models.query import QuerySet

    return QuerySet(model=model, using=using, hints=hints)


def _descriptor_queryset(model, instance):
    """Async equivalent of the descriptors' ``get_queryset(instance=...)``:
    ``model._base_manager.db_manager(hints={"instance": instance}).fetch_mode(
    instance._state.fetch_mode)``.
    """
    queryset = _async_queryset(model, hints={"instance": instance})
    return queryset.fetch_mode(instance._state.fetch_mode)


def _unsupported(prefetcher):
    name = getattr(prefetcher, "name", None)
    if name is None:
        # Descriptors (e.g. GenericForeignKeyDescriptor) wrap the field.
        name = getattr(getattr(prefetcher, "field", None), "name", None)
    if name is None:
        name = type(prefetcher).__name__
    raise NotSupportedError(
        "prefetch_related() for %s is not supported by the async backend."
        % name
    )


def _single_queryset(querysets):
    if querysets and len(querysets) != 1:
        raise ValueError(
            "querysets argument of get_prefetch_querysets() should have a "
            "length of 1."
        )
    return querysets[0] if querysets else None


def _noop(related_objects):
    pass


def _forward_many_to_one(descriptor, instances, querysets):
    """Mirror of ``ForwardManyToOneDescriptor.get_prefetch_querysets()``."""
    field = descriptor.field
    queryset = _single_queryset(querysets)
    cloning_disabled = queryset is None
    if cloning_disabled:
        queryset = _descriptor_queryset(
            field.remote_field.model, instances[0]
        )._disable_cloning()

    rel_obj_attr = field.get_foreign_related_value
    instance_attr = field.get_local_related_value
    instances_dict = {instance_attr(inst): inst for inst in instances}
    remote_field = field.remote_field
    related_fields = [
        queryset.query.resolve_ref(related_field.name).target
        for related_field in field.foreign_related_fields
    ]
    queryset = queryset.filter(
        TupleIn(
            ColPairs(
                queryset.model._meta.db_table,
                related_fields,
                related_fields,
                field,
            ),
            list(instances_dict),
        )
    )
    # There can be only one object prefetched for each instance so clear
    # ordering if the query allows it without side effects.
    queryset.query.clear_ordering()
    # Restore subsequent cloning operations.
    if cloning_disabled:
        queryset._enable_cloning()

    def post_evaluate(related_objects):
        # Since we're going to assign directly in the cache,
        # we must manage the reverse relation cache manually.
        if not remote_field.multiple:
            for rel_obj in related_objects:
                instance = instances_dict[rel_obj_attr(rel_obj)]
                remote_field.set_cached_value(rel_obj, instance)

    return (
        queryset,
        rel_obj_attr,
        instance_attr,
        True,
        field.cache_name,
        False,
        post_evaluate,
    )


def _reverse_one_to_one(descriptor, instances, querysets):
    """Mirror of ``ReverseOneToOneDescriptor.get_prefetch_querysets()``."""
    related = descriptor.related
    queryset = _single_queryset(querysets)
    cloning_disabled = queryset is None
    if cloning_disabled:
        queryset = _descriptor_queryset(
            related.related_model, instances[0]
        )._disable_cloning()

    rel_obj_attr = related.field.get_local_related_value
    instance_attr = related.field.get_foreign_related_value
    instances_dict = {instance_attr(inst): inst for inst in instances}
    query = {"%s__in" % related.field.name: instances}
    queryset = queryset.filter(**query)
    queryset.query.clear_ordering()
    # Restore subsequent cloning operations.
    if cloning_disabled:
        queryset._enable_cloning()

    def post_evaluate(related_objects):
        for rel_obj in related_objects:
            instance = instances_dict[rel_obj_attr(rel_obj)]
            related.field.set_cached_value(rel_obj, instance)

    return (
        queryset,
        rel_obj_attr,
        instance_attr,
        True,
        related.cache_name,
        False,
        post_evaluate,
    )


def _reverse_many_to_one(manager, instances, querysets):
    """Mirror of the reverse FK ``RelatedManager.get_prefetch_querysets()``."""
    field = manager.field
    queryset = _single_queryset(querysets)
    cloning_disabled = queryset is None
    if cloning_disabled:
        queryset = _async_queryset(
            manager.model, using=manager._db, hints=manager._hints
        )._disable_cloning()

    queryset._add_hints(instance=instances[0])
    queryset = queryset.using(queryset._db or manager._db)

    rel_obj_attr = field.get_local_related_value
    instance_attr = field.get_foreign_related_value
    instances_dict = {instance_attr(inst): inst for inst in instances}
    queryset = _filter_prefetch_queryset(queryset, field.name, instances)
    # Restore subsequent cloning operations.
    if cloning_disabled:
        queryset._enable_cloning()

    def post_evaluate(related_objects):
        # Since we just bypassed the manager's get_queryset(), we must manage
        # the reverse relation manually.
        for rel_obj in related_objects:
            if not field.is_cached(rel_obj):
                instance = instances_dict[rel_obj_attr(rel_obj)]
                field.set_cached_value(rel_obj, instance)

    return (
        queryset,
        rel_obj_attr,
        instance_attr,
        False,
        field.remote_field.cache_name,
        False,
        post_evaluate,
    )


def _many_to_many(manager, instances, querysets):
    """Delegate to ``ManyRelatedManager.get_prefetch_querysets()``.

    That implementation only annotates the queryset with the join table
    columns; it never evaluates it. Handing it an async queryset is therefore
    enough to make it async.
    """
    queryset = _single_queryset(querysets)
    if queryset is None:
        queryset = _async_queryset(
            manager.model, using=manager._db, hints=manager._hints
        )

    return (*manager.get_prefetch_querysets(instances, [queryset]), _noop)


async def _generic_foreign_key(descriptor, instances, querysets):
    """Mirror GenericForeignKeyDescriptor without synchronous queries."""
    field = descriptor.field
    custom_querysets = {}
    for queryset in querysets or ():
        ct = await aget_for_model(
            queryset.model,
            using=queryset.db,
            for_concrete_model=field.for_concrete_model,
        )
        if ct.pk in custom_querysets:
            raise ValueError(
                "Only one queryset is allowed for each content type."
            )
        custom_querysets[ct.pk] = queryset

    ct_attname = field.model._meta.get_field(field.ct_field).attname
    foreign_keys = defaultdict(set)
    instance_by_type = {}
    for instance in instances:
        ct_id = getattr(instance, ct_attname)
        fk = getattr(instance, field.fk_field)
        if ct_id is not None and fk is not None:
            foreign_keys[ct_id].add(fk)
            instance_by_type[ct_id] = instance

    models_by_type = {}
    related_querysets = []
    for ct_id, fkeys in foreign_keys.items():
        instance = instance_by_type[ct_id]
        ct = await aget_for_id(ct_id, using=instance._state.db)
        models_by_type[ct_id] = ct.model_class()
        queryset = custom_querysets.get(ct_id)
        if queryset is None:
            queryset = _async_queryset(
                models_by_type[ct_id], using=instance._state.db
            )
        related_querysets.append(
            queryset.filter(pk__in=fkeys).fetch_mode(
                instances[0]._state.fetch_mode
            )
        )

    async def related_objects():
        # One query per content type, all independent: send them in one
        # round trip. Like Django, each target queryset then performs its
        # own nested prefetches (different models may have different ones).
        await aevaluate_pipelined(related_querysets)
        for queryset in related_querysets:
            async for obj in queryset:
                yield obj

    def instance_attr(obj):
        ct_id = getattr(obj, ct_attname)
        fk = getattr(obj, field.fk_field)
        if ct_id is None or fk is None:
            return None
        return str(fk), models_by_type[ct_id]

    return (
        related_objects(),
        lambda obj: (obj._meta.pk.value_to_string(obj), obj.__class__),
        instance_attr,
        True,
        field.name,
        False,
        _noop,
    )


async def _generic_relation(manager, instances, querysets):
    """Mirror GenericRelatedObjectManager.get_prefetch_querysets()."""
    queryset = _single_queryset(querysets)
    if queryset is None:
        queryset = _async_queryset(
            manager.model, using=manager._db, hints=manager._hints
        )
    queryset._add_hints(instance=instances[0])
    queryset = queryset.using(queryset._db or manager._db)

    content_types = {}
    groups = defaultdict(set)
    for instance in instances:
        model = type(instance)
        if model not in content_types:
            content_types[model] = await aget_for_model(
                model,
                using=manager.instance._state.db,
                for_concrete_model=manager.get_content_type.keywords[
                    "for_concrete_model"
                ],
            )
        groups[content_types[model].pk].add(instance.pk)

    query = Q.create(
        [
            Q.create(
                [
                    (f"{manager.content_type_field_name}__pk", ct_id),
                    (f"{manager.object_id_field_name}__in", pks),
                ]
            )
            for ct_id, pks in groups.items()
        ],
        connector=Q.OR,
    )
    object_id_converter = instances[0]._meta.pk.to_python
    ct_attname = manager.model._meta.get_field(
        manager.content_type_field_name
    ).attname
    return (
        queryset.filter(query),
        lambda obj: (
            object_id_converter(getattr(obj, manager.object_id_field_name)),
            getattr(obj, ct_attname),
        ),
        lambda obj: (obj.pk, content_types[type(obj)].pk),
        False,
        manager.prefetch_cache_name,
        False,
        _noop,
    )


async def _get_prefetch_querysets(prefetcher, instances, querysets):
    """Build the related queryset for ``prefetcher`` without evaluating it.

    Return Django's ``get_prefetch_querysets()`` six-tuple plus a
    ``post_evaluate(related_objects)`` callback.
    """
    if isinstance(prefetcher, ForwardManyToOneDescriptor):
        # Also covers ForwardOneToOneDescriptor.
        return _forward_many_to_one(prefetcher, instances, querysets)

    if isinstance(prefetcher, ReverseOneToOneDescriptor):
        return _reverse_one_to_one(prefetcher, instances, querysets)

    # Related managers are built by factories in related_descriptors, so the
    # generated classes are the only reliable marker.
    if type(prefetcher).__module__ == RELATED_DESCRIPTORS_MODULE:
        if hasattr(prefetcher, "through"):
            return _many_to_many(prefetcher, instances, querysets)
        return _reverse_many_to_one(prefetcher, instances, querysets)

    if apps.is_installed("django.contrib.contenttypes"):
        from django.contrib.contenttypes.fields import (
            GenericForeignKeyDescriptor,
        )

        if isinstance(prefetcher, GenericForeignKeyDescriptor):
            return await _generic_foreign_key(prefetcher, instances, querysets)
        if type(prefetcher).__module__ == CONTENTTYPES_MODULE:
            return await _generic_relation(prefetcher, instances, querysets)

    # Custom descriptors and managers opt in by implementing an async twin
    # of Django's hook. It must return the same six-tuple, with an *async*
    # queryset (or async iterable) in the first slot, and must not evaluate
    # it. Discovery still goes through Django's get_prefetcher(), so the
    # sync ``get_prefetch_querysets`` and ``is_cached`` must exist too.
    hook = getattr(prefetcher, "aget_prefetch_querysets", None)
    if hook is not None:
        return (*await hook(instances, querysets), _noop)

    return _unsupported(prefetcher)


async def _aplan_one_level(instances, prefetcher, lookup, level):
    """
    Build (but don't evaluate) the related queryset for one prefetch level.

    Return the seven-tuple of _get_prefetch_querysets() and the additional
    lookups stripped from the queryset.
    """
    plan = await _get_prefetch_querysets(
        prefetcher, instances, lookup.get_current_querysets(level)
    )
    rel_qs = plan[0]
    # We have to handle the possibility that the QuerySet we just got back
    # contains some prefetch_related lookups. We don't want to trigger the
    # prefetch_related functionality by evaluating the query. Rather, we need
    # to merge in the prefetch_related lookups.
    # Copy the lookups in case it is a Prefetch object which could be reused
    # later (happens in nested prefetch_related).
    additional_lookups = [
        copy.copy(additional_lookup)
        for additional_lookup in getattr(
            rel_qs, "_prefetch_related_lookups", ()
        )
    ]
    if additional_lookups:
        # Don't need to clone because the manager should have given us a fresh
        # instance, so we access an internal instead of using public interface
        # for performance reasons.
        rel_qs._prefetch_related_lookups = ()
    return plan, additional_lookups


# Top-level levels planned and evaluated ahead by aprefetch_top_level(),
# keyed by prefetch_to; prefetch_one_level() consumes them.
_planned_levels = contextvars.ContextVar(
    "django_async_backend_planned_levels", default=None
)


def _take_planned(instances, lookup, level):
    planned = _planned_levels.get()
    if not planned or level != 0:
        return None
    entry = planned.get(lookup.get_current_prefetch_to(level))
    if entry is None:
        return None
    planned_instances, plan, additional_lookups = entry
    if len(planned_instances) != len(instances) or any(
        a is not b for a, b in zip(planned_instances, instances)
    ):
        return None
    del planned[lookup.get_current_prefetch_to(level)]
    return plan, additional_lookups


async def aprefetch_top_level(model_instances, related_lookups):
    """
    Plan the first level of every lookup in ``related_lookups`` and send the
    resulting querysets in one pipeline, so ``prefetch_related("a", "b",
    "c")`` costs one round trip for its three queries instead of three.

    Mirrors the preamble of prefetch_related_objects() for level 0 (same
    processing order, same skips). A lookup this can't plan is left to the
    normal loop, which also raises any error for it. Return the planned
    levels for prefetch_one_level() to consume.
    """
    planned = {}
    if not model_instances or not pipeline_enabled():
        return planned

    for obj in model_instances:
        if not hasattr(obj, "_prefetched_objects_cache"):
            try:
                obj._prefetched_objects_cache = {}
            except (AttributeError, TypeError):
                return planned

    from django_async_backend.db.models.query import normalize_prefetch_lookups

    first_obj = next(iter(model_instances))
    lookups = normalize_prefetch_lookups(reversed(related_lookups))
    candidates = []
    seen = set()
    # prefetch_related_objects() pops from the end of this list.
    for lookup in reversed(lookups):
        prefetch_to = lookup.get_current_prefetch_to(0)
        if prefetch_to in seen:
            continue
        seen.add(prefetch_to)
        through_attr = lookup.prefetch_through.split(LOOKUP_SEP)[0]
        to_attr = lookup.get_current_to_attr(0)[0]
        prefetcher, _descriptor, attr_found, is_fetched = await get_prefetcher(
            first_obj, through_attr, to_attr
        )
        if not attr_found or prefetcher is None:
            continue
        obj_to_fetch = [obj for obj in model_instances if not is_fetched(obj)]
        if obj_to_fetch:
            candidates.append((prefetch_to, obj_to_fetch, prefetcher, lookup))

    if len(candidates) < 2:
        return planned

    for prefetch_to, obj_to_fetch, prefetcher, lookup in candidates:
        plan, additional_lookups = await _aplan_one_level(
            obj_to_fetch, prefetcher, lookup, 0
        )
        planned[prefetch_to] = (obj_to_fetch, plan, additional_lookups)

    await aevaluate_pipelined([entry[1][0] for entry in planned.values()])
    return planned


def wrap_prefetch_related_objects(prefetch_related_objects):
    """Run aprefetch_top_level() ahead of the generated loop."""

    async def pipelined_prefetch_related_objects(
        model_instances, *related_lookups
    ):
        if not model_instances:
            return
        token = _planned_levels.set(
            await aprefetch_top_level(model_instances, related_lookups)
        )
        try:
            return await prefetch_related_objects(
                model_instances, *related_lookups
            )
        finally:
            _planned_levels.reset(token)

    pipelined_prefetch_related_objects.__wrapped__ = prefetch_related_objects
    pipelined_prefetch_related_objects.__name__ = "prefetch_related_objects"
    pipelined_prefetch_related_objects.__qualname__ = (
        "prefetch_related_objects"
    )
    return pipelined_prefetch_related_objects


async def prefetch_one_level(instances, prefetcher, lookup, level):
    """
    Helper function for prefetch_related_objects().

    Run prefetches on all instances using the prefetcher object,
    assigning results to relevant caches in instance.

    Return the prefetched objects along with any additional prefetches that
    must be done due to prefetch_related lookups found from default managers.
    """
    planned = _take_planned(instances, lookup, level)
    if planned is None:
        planned = await _aplan_one_level(instances, prefetcher, lookup, level)
    (
        (
            rel_qs,
            rel_obj_attr,
            instance_attr,
            single,
            cache_name,
            is_descriptor,
            post_evaluate,
        ),
        additional_lookups,
    ) = planned

    all_related_objects = [rel_obj async for rel_obj in rel_qs]
    # The sync descriptors fill the reverse relation caches while iterating;
    # do it now that the queryset has been consumed.
    post_evaluate(all_related_objects)

    rel_obj_cache = {}
    for rel_obj in all_related_objects:
        rel_attr_val = rel_obj_attr(rel_obj)
        rel_obj_cache.setdefault(rel_attr_val, []).append(rel_obj)

    to_attr, as_attr = lookup.get_current_to_attr(level)
    # Make sure `to_attr` does not conflict with a field.
    if as_attr and instances:
        # We assume that objects retrieved are homogeneous (which is the
        # premise of prefetch_related), so what applies to first object applies
        # to all.
        model = instances[0].__class__
        try:
            model._meta.get_field(to_attr)
        except exceptions.FieldDoesNotExist:
            pass
        else:
            raise ValueError(
                "to_attr=%s conflicts with a field on the %s model."
                % (to_attr, model.__name__)
            )

    for obj in instances:
        instance_attr_val = instance_attr(obj)
        vals = rel_obj_cache.get(instance_attr_val, [])

        if single:
            val = vals[0] if vals else None
            if as_attr:
                # A to_attr has been given for the prefetch.
                setattr(obj, to_attr, val)
            elif is_descriptor:
                # cache_name points to a field name in obj.
                # This field is a descriptor for a related object.
                setattr(obj, cache_name, val)
            else:
                # No to_attr has been given for this prefetch operation and the
                # cache_name does not point to a descriptor. Store the value of
                # the field in the object's field cache.
                obj._state.fields_cache[cache_name] = val
        else:
            if as_attr:
                setattr(obj, to_attr, vals)
            else:
                manager = getattr(obj, to_attr)
                # Related managers expose Django's synchronous queryset API.
                # Keep the cache shape consistent even when an async custom
                # queryset supplied the prefetched values.
                qs = manager.get_queryset()
                qs._result_cache = vals
                # We don't want the individual qs doing prefetch_related now,
                # since we have merged this into the current work.
                qs._prefetch_done = True
                obj._prefetched_objects_cache[cache_name] = qs
    return all_related_objects, additional_lookups
