"""Async twins of the accessors Django attaches to a model class.

Django's ``Model`` class body is covered by ``AsyncModelMixin`` (generated);
three families of methods are attached *per model* instead, as
``functools.partialmethod`` objects, and this module mirrors each of them:

- ``get_next_by_<field>()`` / ``get_previous_by_<field>()`` for every
  non-null ``DateField`` / ``DateTimeField`` → ``aget_next_by_<field>()`` /
  ``aget_previous_by_<field>()``.
- ``get_next_in_order()`` / ``get_previous_in_order()`` on a model with
  ``Meta.order_with_respect_to`` → ``aget_next_in_order()`` /
  ``aget_previous_in_order()``.
- ``get_<model>_order()`` / ``set_<model>_order()`` on the model that is
  ordered *with respect to* → ``aget_<model>_order()`` (returns the list of
  primary keys) / ``aset_<model>_order()``.

The accessors are added for every model at startup and for models prepared
later (``class_prepared``), so third-party models get them too. The async
bodies live on the mixin (``_async_get_next_or_previous_by_FIELD`` and
``_async_get_next_or_previous_in_order``) and here (the order accessors).
"""

from functools import partialmethod

from django.apps import apps
from django.db.models import Model
from django.db.models.base import (
    method_get_order,
    method_set_order,
)
from django.db.models.fields.related import lazy_related_operation
from django.db.models.signals import class_prepared

DISPATCH_UID = "django_async_backend.db.models.ordering"


async def amethod_set_order(self, ordered_obj, id_list, using=None):
    order_wrt = ordered_obj._meta.order_with_respect_to
    filter_args = order_wrt.get_forward_related_filter(self)
    await (
        ordered_obj.async_objects.db_manager(using)
        .filter(**filter_args)
        .abulk_update(
            [
                ordered_obj(pk=pk, _order=order)
                for order, pk in enumerate(id_list)
            ],
            ["_order"],
        )
    )


async def amethod_get_order(self, ordered_obj):
    order_wrt = ordered_obj._meta.order_with_respect_to
    filter_args = order_wrt.get_forward_related_filter(self)
    pk_name = ordered_obj._meta.pk.name
    return [
        pk
        async for pk in ordered_obj.async_objects.filter(
            **filter_args
        ).values_list(pk_name, flat=True)
    ]


def make_async_foreign_order_accessors(model, related_model):
    setattr(
        related_model,
        "aget_%s_order" % model.__name__.lower(),
        partialmethod(amethod_get_order, model),
    )
    setattr(
        related_model,
        "aset_%s_order" % model.__name__.lower(),
        partialmethod(amethod_set_order, model),
    )


def add_async_accessors(model):
    """Attach the ``a``-twins of the per-model accessors ``model`` has.

    Each sync accessor is a ``partialmethod`` of one of Django's two helper
    functions; the twin is the same ``partialmethod`` over the mixin's async
    helper, so custom fields that reuse Django's mechanism are covered too.
    """
    by_field = Model._get_next_or_previous_by_FIELD
    in_order = Model._get_next_or_previous_in_order
    ordered = False
    for name, value in list(vars(model).items()):
        if not isinstance(value, partialmethod):
            continue
        if value.func is by_field:
            setattr(
                model,
                "a" + name,
                partialmethod(
                    model._async_get_next_or_previous_by_FIELD,
                    *value.args,
                    **value.keywords,
                ),
            )
        elif value.func is in_order:
            ordered = True
            setattr(
                model,
                "a" + name,
                partialmethod(
                    model._async_get_next_or_previous_in_order,
                    *value.args,
                    **value.keywords,
                ),
            )
        elif value.func in (method_get_order, method_set_order):
            # Django attached get_<x>_order/set_<x>_order to this model
            # on behalf of the ordered model in value.args[0].
            make_async_foreign_order_accessors(value.args[0], model)
    if ordered:
        wrt = model._meta.order_with_respect_to
        if wrt is not None and wrt.remote_field:
            lazy_related_operation(
                make_async_foreign_order_accessors,
                model,
                wrt.remote_field.model,
            )


def _on_class_prepared(sender, **kwargs):
    add_async_accessors(sender)


_installed = False


def install():
    global _installed
    if _installed:  # pragma: no cover
        return
    for model in apps.get_models(include_auto_created=True):
        add_async_accessors(model)
    class_prepared.connect(
        _on_class_prepared, weak=False, dispatch_uid=DISPATCH_UID
    )
    _installed = True
