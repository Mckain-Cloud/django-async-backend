"""model_bakery on the async connection.

``baker.prepare()`` builds instances (and, with ``_save_related=False``,
their unsaved related instances) without touching the database. The
functions here prepare and then save the graph with ``async_save()``::

    from django_async_backend.contrib import model_bakery as abaker

    book = await abaker.amake(Book, title="Dune")
    books = await abaker.amake(Book, _quantity=3, author=author)
    owner = await abaker.amake(Owner, tags=[tag1, tag2], make_m2m=True)
    book = await abaker.amake_recipe("catalog.book")

Supported: field generation, ``_quantity``, ``_fill_optional``, explicit and
generated foreign keys (``make_m2m`` generates related rows too), ``tags=[...]``
for many-to-many fields, reverse one-to-many values, reverse one-to-one
values, generic foreign keys given a saved instance, auto_now overrides,
``_refresh_after_create``, ``_using``, recipes with ``foreign_key()``,
``related()``, ``seq()``/``cycle()``.

Not supported: ``_bulk_create``, ``_from_manager``, ``_create_files`` on the
related models, and recipe attributes that are callables doing their own
database work.
"""

import collections.abc
import copy
import itertools

from django.db import DEFAULT_DB_ALIAS
from django.db.models import Model
from django.db.models.fields.related import (
    ForeignObjectRel,
    ManyToManyField,
)
from django.db.models.fields.reverse_related import OneToOneRel
from model_bakery import baker as _baker
from model_bakery.baker import (
    MAX_MANY_QUANTITY,
    Baker,
    _recipe,
    _valid_quantity,
)
from model_bakery.exceptions import InvalidQuantityException
from model_bakery.recipe import (
    Recipe,
    RecipeForeignKey,
    related,
)

from django_async_backend.db.models.graph import asave_graph

__all__ = ["amake", "aprepare", "amake_recipe", "aprepare_recipe"]

aprepare = _baker.prepare
aprepare_recipe = _baker.prepare_recipe


class _AsyncBaker(Baker):
    """``make_m2m`` must prepare the related rows, not make them: they are
    saved on the async connection afterwards.
    """

    def m2m_value(self, field):
        explicit = field.name in self.rel_fields
        if not explicit and (
            not self.make_m2m or field.null and not field.fill_optional
        ):
            return []
        # model_bakery's m2m generator has no prepare() variant.
        attrs = (
            _baker.filter_rel_attrs(field.name, **self.rel_attrs)
            if explicit
            else {}
        )
        return _baker.prepare(
            self._remote_field(field).model,
            _quantity=MAX_MANY_QUANTITY,
            _using=self._using,
            **attrs,
        )


def _is_auto_now(field):
    return getattr(field, "auto_now", False) or getattr(
        field, "auto_now_add", False
    )


def _split_attrs(model, attrs):
    """Pull the values model_bakery applies after save out of ``attrs``:
    reverse relations, many-to-many values and auto_now overrides.
    """
    reverse_many = {}
    reverse_one = {}
    m2m = {}
    auto_now = {}
    for name in tuple(attrs):
        try:
            field = model._meta.get_field(name)
        except Exception:
            continue
        if isinstance(field, OneToOneRel):
            reverse_one[name] = attrs.pop(name)
        elif isinstance(field, ForeignObjectRel):
            reverse_many[name] = attrs.pop(name)
        elif isinstance(field, ManyToManyField):
            m2m[name] = attrs.pop(name)
        elif _is_auto_now(field):
            auto_now[name] = attrs[name]
    return reverse_many, reverse_one, m2m, auto_now


def _call(value, **kwargs):
    if callable(value) and not isinstance(value, Model):
        return value(**kwargs)
    return value


async def _asave_values(values, using):
    objs = list(values)
    for obj in objs:
        if isinstance(obj, Model):
            await asave_graph(obj, using=using)
    return objs


async def _afinish(
    instance,
    *,
    baker,
    reverse_many,
    reverse_one,
    m2m,
    auto_now,
    using,
    make_m2m,
):
    await asave_graph(instance, using=using)

    for name, values in reverse_many.items():
        manager = getattr(instance, name).async_objects
        values = list(_call(values))
        # Point the children at this instance before saving them, so a
        # related instance prepare() generated for the foreign key is
        # dropped instead of being saved as an orphan.
        fk_name = manager.field.name
        for value in values:
            setattr(value, fk_name, instance)
        values = await _asave_values(values, using)
        await manager.aset(values, clear=True)

    for name, value in reverse_one.items():
        value = _call(value)
        if value is not None:
            setattr(instance, name, value)
            await asave_graph(value, using=using)

    # Values the baker generated or that were passed for m2m fields.
    m2m_values = dict(baker.m2m_dict)
    m2m_values.update(m2m)
    for name, values in m2m_values.items():
        values = _call(values)
        if values is None:
            continue
        values = await _asave_values(values, using)
        if not values:
            continue
        relation = getattr(instance, name)
        if relation.through._meta.auto_created:
            await relation.async_objects.aadd(*values)
        else:
            for value in values:
                await amake(
                    relation.through,
                    _using=using or "",
                    **{
                        relation.source_field_name: instance,
                        relation.target_field_name: value,
                    },
                )

    if auto_now:
        manager = type(instance).async_objects
        db = using or instance._state.db
        if db:
            manager = manager.using(db)
        await manager.filter(pk=instance.pk).aupdate(**auto_now)
        for key, value in auto_now.items():
            setattr(instance, key, value)
    return instance


async def amake(
    _model,
    _quantity=None,
    make_m2m=False,
    _refresh_after_create=False,
    _create_files=False,
    _using="",
    _fill_optional=False,
    _full_clean=False,
    **attrs,
):
    """Async ``baker.make()``: prepare, then save on the async connection."""
    if _valid_quantity(_quantity):
        raise InvalidQuantityException
    using = _using or None

    async def one():
        baker = _AsyncBaker.create(
            _model,
            make_m2m=make_m2m,
            create_files=_create_files,
            _using=_using,
        )
        local_attrs = dict(attrs)
        reverse_many, reverse_one, m2m, auto_now = _split_attrs(
            baker.model, local_attrs
        )
        prepare_kwargs = {"_fill_optional": _fill_optional}
        if _full_clean:
            prepare_kwargs["_full_clean"] = True
        instance = baker.prepare(
            _save_related=False, **prepare_kwargs, **local_attrs
        )
        instance = await _afinish(
            instance,
            baker=baker,
            reverse_many=reverse_many,
            reverse_one=reverse_one,
            m2m=m2m,
            auto_now=auto_now,
            using=using,
            make_m2m=make_m2m,
        )
        if _refresh_after_create:
            await instance.async_refresh_from_db()
        return instance

    if _quantity:
        return [await one() for _ in range(_quantity)]
    return await one()


async def _amapping(recipe, _using, new_attrs):
    """Async twin of ``Recipe._mapping()``: the same resolution of iterators,
    ``foreign_key()`` and ``related()`` entries, with the one database check
    (does the table have rows, to decide whether iterators restart) done on
    the async connection and related recipes prepared, not made.
    """
    _quantity = (
        new_attrs.get("_quantity", recipe.attr_mapping.get("_quantity")) or 1
    )
    rel_fields_attrs = {k: v for k, v in new_attrs.items() if "__" in k}
    new_attrs = {k: v for k, v in new_attrs.items() if "__" not in k}
    model = (
        _baker.finder.get_model(recipe._model)
        if isinstance(recipe._model, str)
        else recipe._model
    )
    mapping = recipe.attr_mapping.copy()
    deferred_related = {}
    for k, v in recipe.attr_mapping.items():
        if k in new_attrs:
            continue
        elif isinstance(v, collections.abc.Iterator):
            if k not in recipe._iterator_backups or not (
                await model.async_objects.using(_using or None).aexists()
            ):
                recipe._iterator_backups[k] = itertools.tee(
                    recipe._iterator_backups.get(k, [v])[0]
                )
            mapping[k] = recipe._iterator_backups[k][1]
        elif isinstance(v, RecipeForeignKey):
            attrs = {}
            for key in list(rel_fields_attrs):
                if key.startswith(f"{k}__"):
                    attrs[key] = rel_fields_attrs.pop(key)
            recipe_attrs = _baker.filter_rel_attrs(k, **attrs)
            descriptor = getattr(model, k, None)
            if type(descriptor).__name__ == "ReverseOneToOneDescriptor":
                # A reverse one-to-one needs the parent saved first.
                deferred_related[k] = (v.recipe, recipe_attrs)
                mapping.pop(k, None)
            elif v.one_to_one:
                prepared = [
                    await _aprepare_recipe(v.recipe, _using, recipe_attrs)
                    for _ in range(_quantity)
                ]
                mapping[k] = itertools.cycle(prepared)
            else:
                mapping[k] = await _aprepare_recipe(
                    v.recipe, _using, recipe_attrs
                )
        elif isinstance(v, related):
            deferred_related[k] = v
            mapping.pop(k, None)
        elif isinstance(v, collections.abc.Container):
            mapping[k] = copy.deepcopy(v)

    mapping.update(new_attrs)
    mapping.update(rel_fields_attrs)
    for option in (
        "make_m2m",
        "_refresh_after_create",
        "_create_files",
        "_bulk_create",
        "_save_kwargs",
        "_save_related",
        "_quantity",
    ):
        mapping.pop(option, None)
    return mapping, deferred_related


async def _aprepare_recipe(recipe, _using, attrs):
    """``Recipe.prepare()`` with the mapping resolved asynchronously (its
    iterator bookkeeping queries the table)."""
    mapping, _deferred = await _amapping(recipe, _using, dict(attrs))
    return _baker.prepare(recipe._model, _using=_using, **mapping)


async def _arelated(instance, deferred_related, using):
    for name, value in deferred_related.items():
        descriptor = getattr(type(instance), name)
        if isinstance(value, tuple):  # reverse one-to-one recipe
            rel_recipe, recipe_attrs = value
            fk_name = descriptor.related.field.name
            obj = await _aprepare_recipe(
                rel_recipe, using or "", {**recipe_attrs, fk_name: instance}
            )
            await asave_graph(obj, using=using)
            setattr(instance, name, obj)
            continue
        manager = getattr(instance, name)
        fk_name = manager.field.name
        for rel_recipe in value.related:
            objs = await amake_recipe_obj(
                rel_recipe, _using=using or "", **{fk_name: instance}
            )
            if not isinstance(objs, list):
                objs = [objs]


async def amake_recipe_obj(recipe, _quantity=None, _using="", **new_attrs):
    """Async ``Recipe.make()`` for a ``Recipe`` object."""
    using = _using or None
    defaults = {}
    if _quantity is not None:
        defaults["_quantity"] = _quantity
    defaults.update(new_attrs)

    async def one():
        mapping, deferred_related = await _amapping(recipe, _using, defaults)
        instance = await amake(recipe._model, _using=_using, **mapping)
        await _arelated(instance, deferred_related, using)
        return instance

    if _quantity:
        return [await one() for _ in range(_quantity)]
    return await one()


async def amake_recipe(
    baker_recipe_name, _quantity=None, _using="", **new_attrs
):
    """Async ``baker.make_recipe()``."""
    recipe = (
        baker_recipe_name
        if isinstance(baker_recipe_name, Recipe)
        else _recipe(baker_recipe_name)
    )
    return await amake_recipe_obj(
        recipe, _quantity=_quantity, _using=_using, **new_attrs
    )
