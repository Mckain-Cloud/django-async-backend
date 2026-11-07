# This file was generated automatically. Do not modify it manually. (based on django 6.1)
import copy
import inspect
import warnings
from collections import defaultdict
from functools import partialmethod
from itertools import chain

import django
from asgiref.sync import sync_to_async
from django.apps import apps
from django.conf import settings
from django.core import checks
from django.core.exceptions import (
    NON_FIELD_ERRORS,
    FieldDoesNotExist,
    FieldError,
    MultipleObjectsReturned,
    ObjectDoesNotExist,
    ObjectNotUpdated,
    ValidationError,
)
from django.db import (
    DJANGO_VERSION_PICKLE_KEY,
    DatabaseError,
    connection,
    router,
    transaction,
)
from django.db.models import (
    NOT_PROVIDED,
    ExpressionWrapper,
    IntegerField,
    Max,
    Value,
)
from django.db.models.base import ModelBase
from django.db.models.constants import LOOKUP_SEP
from django.db.models.deletion import CASCADE
from django.db.models.expressions import DatabaseDefault
from django.db.models.fields.composite import CompositePrimaryKey
from django.db.models.fields.related import (
    ForeignObjectRel,
    OneToOneField,
    lazy_related_operation,
    resolve_relation,
)
from django.db.models.functions import Coalesce
from django.db.models.manager import Manager
from django.db.models.options import Options
from django.db.models.query import (
    F,
    Q,
)
from django.db.models.signals import (
    class_prepared,
    post_init,
    post_save,
    pre_init,
    pre_save,
)
from django.db.models.utils import (
    AltersData,
    make_model_tuple,
)
from django.utils.encoding import force_str
from django.utils.functional import classproperty
from django.utils.hashable import make_hashable
from django.utils.text import (
    capfirst,
    get_text_list,
)
from django.utils.translation import gettext_lazy as _

from django_async_backend.db import async_connections
from django_async_backend.db.models.constraints import avalidate_constraint
from django_async_backend.db.models.deletion import Collector
from django_async_backend.db.models.manager import AsyncManager
from django_async_backend.db.transaction import (
    async_atomic,
    async_mark_for_rollback_on_error,
)


class AsyncModelMixin:
    async def _async_clean(self):
        # Model.aclean() may query; Model.clean() must not.
        if hasattr(self, "aclean"):
            return await self.aclean()
        return self.clean()

    @classproperty
    def async_objects(cls):
        manager = cls.__dict__.get("_async_objects_cache")
        if manager is None:
            manager = AsyncManager()
            manager.name = "async_objects"
            manager.model = cls
            cls._async_objects_cache = manager
        return manager

    @classproperty
    def _async_base_manager(cls):
        manager = cls.__dict__.get("_async_base_manager_cache")
        if manager is None:
            manager = AsyncManager()
            manager.name = "_async_base_manager"
            manager.model = cls
            manager.auto_created = True
            cls._async_base_manager_cache = manager
        return manager

    async def async_refresh_from_db(
        self, using=None, fields=None, from_queryset=None
    ):
        from django_async_backend.db.models.convert import to_async_queryset

        if fields is None:
            self._prefetched_objects_cache = {}
        else:
            prefetched_objects_cache = getattr(
                self, "_prefetched_objects_cache", ()
            )
            fields = set(fields)
            for field in fields.copy():
                if field in prefetched_objects_cache:
                    del prefetched_objects_cache[field]
                    fields.remove(field)
            if not fields:
                return
            if any(LOOKUP_SEP in f for f in fields):
                raise ValueError(
                    'Found "%s" in fields argument. Relations and '
                    "transforms are not allowed in fields." % LOOKUP_SEP
                )

        if from_queryset is None:
            hints = {"instance": self}
            from_queryset = self.__class__._async_base_manager.db_manager(
                using, hints=hints
            )
        else:
            from_queryset = to_async_queryset(from_queryset)
            if using is not None:
                from_queryset = from_queryset.using(using)

        db_instance_qs = from_queryset.filter(pk=self.pk)
        if fields is not None:
            db_instance_qs = db_instance_qs.only(*fields)

        db_instance = await db_instance_qs.aget()
        non_loaded_fields = db_instance.get_deferred_fields()
        for field in self._meta.fields:
            if field.attname in non_loaded_fields:
                # This field wasn't refreshed - skip ahead.
                continue
            if field.concrete:
                setattr(
                    self, field.attname, getattr(db_instance, field.attname)
                )
            # Clear or copy cached foreign keys.
            if field.is_relation:
                if field.is_cached(db_instance):
                    field.set_cached_value(
                        self, field.get_cached_value(db_instance)
                    )
                elif field.is_cached(self):
                    field.delete_cached_value(self)

        # Clear cached relations.
        for rel in self._meta.related_objects:
            if (fields is None or rel.name in fields) and rel.is_cached(self):
                rel.delete_cached_value(self)

        # Clear cached private relations.
        for field in self._meta.private_fields:
            if (
                (fields is None or field.name in fields)
                and field.is_relation
                and field.is_cached(self)
            ):
                field.delete_cached_value(self)

        self._state.db = db_instance._state.db

    async def _async_get_next_or_previous_by_FIELD(
        self, field, is_next, **kwargs
    ):
        # Behind aget_next_by_<field>() / aget_previous_by_<field>(),
        # attached per model by db/models/ordering.py.
        if not self._async_is_pk_set():
            raise ValueError(
                "get_next/get_previous cannot be used on unsaved objects."
            )
        op = "gt" if is_next else "lt"
        order = "" if is_next else "-"
        param = getattr(self, field.attname)
        q = Q.create(
            [(field.name, param), (f"pk__{op}", self.pk)], connector=Q.AND
        )
        q = Q.create([q, (f"{field.name}__{op}", param)], connector=Q.OR)
        qs = (
            self.__class__.async_objects.using(self._state.db)
            .filter(**kwargs)
            .filter(q)
            .order_by("%s%s" % (order, field.name), "%spk" % order)
        )
        obj = await qs.afirst()
        if obj is None:
            raise self.DoesNotExist(
                "%s matching query does not exist."
                % self.__class__._meta.object_name
            )
        return obj

    async def _async_get_next_or_previous_in_order(self, is_next):
        # Behind aget_next_in_order() / aget_previous_in_order() of a
        # model with order_with_respect_to (db/models/ordering.py).
        cachename = "__%s_order_cache" % is_next
        if not hasattr(self, cachename):
            op = "gt" if is_next else "lt"
            order = "_order" if is_next else "-_order"
            order_field = self._meta.order_with_respect_to
            filter_args = order_field.get_filter_kwargs_for_object(self)
            manager = self.__class__.async_objects
            obj = await (
                manager.filter(**filter_args)
                .filter(
                    **{
                        "_order__%s"
                        % op: manager.values("_order").filter(
                            **{self._meta.pk.name: self.pk}
                        )
                    }
                )
                .order_by(order)[:1]
                .aget()
            )
            setattr(self, cachename, obj)
        return getattr(self, cachename)

    def _async_get_pk_val(self, meta=None):
        meta = meta or self._meta
        return getattr(self, meta.pk.attname)

    def _async_is_pk_set(self, meta=None):
        pk_val = self._async_get_pk_val(meta)

        def _is_unset(value):
            return (
                value is None
                # Empty value when db_default is used.
                or isinstance(value, DatabaseDefault)
            )

        return not (
            _is_unset(pk_val)
            or (
                isinstance(pk_val, tuple) and any(_is_unset(f) for f in pk_val)
            )
        )

    async def async_save(
        self,
        *,
        force_insert=False,
        force_update=False,
        using=None,
        update_fields=None,
    ):

        await self._async_prepare_related_fields_for_save(
            operation_name="save"
        )

        using = using or router.db_for_write(self.__class__, instance=self)
        if force_insert and (force_update or update_fields):
            raise ValueError(
                "Cannot force both insert and updating in model saving."
            )

        deferred_non_generated_fields = {
            f.attname
            for f in self._meta.concrete_fields
            if f.attname not in self.__dict__ and f.generated is False
        }
        if update_fields is not None:
            # If update_fields is empty, skip the save. We do also check for
            # no-op saves later on for inheritance cases. This bailout is
            # still needed for skipping signal sending.
            if not update_fields:
                return

            update_fields = frozenset(update_fields)
            field_names = self._meta._non_pk_concrete_field_names
            not_updatable_fields = update_fields.difference(field_names)

            if not_updatable_fields:
                raise ValueError(
                    "The following fields do not exist in this model, are m2m "
                    "fields, primary keys, or are non-concrete fields: %s"
                    % ", ".join(not_updatable_fields)
                )

        # If saving to the same database, and this model is deferred, then
        # automatically do an "update_fields" save on the loaded fields.
        elif (
            not force_insert
            and deferred_non_generated_fields
            and using == self._state.db
            and self._async_is_pk_set()
        ):
            field_names = set()
            pk_fields = self._meta.pk_fields
            for field in self._meta.concrete_fields:
                if field not in pk_fields and not hasattr(field, "through"):
                    field_names.add(field.attname)
            loaded_fields = field_names.difference(
                deferred_non_generated_fields
            )
            if loaded_fields:
                update_fields = frozenset(loaded_fields)

        await self._async_save_base(
            using=using,
            force_insert=force_insert,
            force_update=force_update,
            update_fields=update_fields,
        )

    async_save.alters_data = True

    @classmethod
    def _async_validate_force_insert(cls, force_insert):
        if force_insert is False:
            return ()
        if force_insert is True:
            return (cls,)
        if not isinstance(force_insert, tuple):
            raise TypeError("force_insert must be a bool or tuple.")
        for member in force_insert:
            if not isinstance(member, ModelBase):
                raise TypeError(
                    f"Invalid force_insert member. {member!r} must be a model subclass."
                )
            if not issubclass(cls, member):
                raise TypeError(
                    f"Invalid force_insert member. {member.__qualname__} must be a "
                    f"base of {cls.__qualname__}."
                )
        return force_insert

    async def _async_save_base(
        self,
        raw=False,
        force_insert=False,
        force_update=False,
        using=None,
        update_fields=None,
    ):
        using = using or router.db_for_write(self.__class__, instance=self)
        assert not (force_insert and (force_update or update_fields))
        assert update_fields is None or update_fields
        cls = origin = self.__class__
        # Skip proxies, but keep the origin as the proxy model.
        if cls._meta.proxy:
            cls = cls._meta.concrete_model
        meta = cls._meta
        if not meta.auto_created:
            await pre_save.asend(
                sender=origin,
                instance=self,
                raw=raw,
                using=using,
                update_fields=update_fields,
            )
        # A transaction isn't needed if one query is issued.
        if meta.parents:
            context_manager = async_atomic(using=using, savepoint=False)
        else:
            context_manager = async_mark_for_rollback_on_error(using=using)
        async with context_manager:
            parent_inserted = False
            if not raw:
                # Validate force insert only when parents are inserted.
                force_insert = self._async_validate_force_insert(force_insert)
                parent_inserted = await self._async_save_parents(
                    cls, using, update_fields, force_insert
                )
            updated = await self._async_save_table(
                raw,
                cls,
                force_insert or parent_inserted,
                force_update,
                using,
                update_fields,
            )
        # Store the database on which the object was saved
        self._state.db = using
        # Once saved, this is no longer a to-be-added instance.
        self._state.adding = False

        # Signal that the save is complete
        if not meta.auto_created:
            await post_save.asend(
                sender=origin,
                instance=self,
                created=(not updated),
                update_fields=update_fields,
                raw=raw,
                using=using,
            )

    _async_save_base.alters_data = True

    async def _async_save_parents(
        self, cls, using, update_fields, force_insert, updated_parents=None
    ):
        meta = cls._meta
        inserted = False
        if updated_parents is None:
            updated_parents = {}
        for parent, field in meta.parents.items():
            # Make sure the link fields are synced between parent and self.
            if (
                field
                and getattr(self, parent._meta.pk.attname) is None
                and getattr(self, field.attname) is not None
            ):
                setattr(
                    self, parent._meta.pk.attname, getattr(self, field.attname)
                )
            if (parent_updated := updated_parents.get(parent)) is None:
                parent_inserted = await self._async_save_parents(
                    cls=parent,
                    using=using,
                    update_fields=update_fields,
                    force_insert=force_insert,
                    updated_parents=updated_parents,
                )
                updated = await self._async_save_table(
                    cls=parent,
                    using=using,
                    update_fields=update_fields,
                    force_insert=parent_inserted
                    or issubclass(parent, force_insert),
                )
                if not updated:
                    inserted = True
                updated_parents[parent] = updated
            elif not parent_updated:
                inserted = True
            # Set the parent's PK value to self.
            if field:
                setattr(
                    self, field.attname, self._async_get_pk_val(parent._meta)
                )
                # Since we didn't have an instance of the parent handy set
                # attname directly, bypassing the descriptor. Invalidate
                # the related object cache, in case it's been accidentally
                # populated. A fresh instance will be re-built from the
                # database if necessary.
                if field.is_cached(self):
                    field.delete_cached_value(self)
        return inserted

    async def _async_save_table(
        self,
        raw=False,
        cls=None,
        force_insert=False,
        force_update=False,
        using=None,
        update_fields=None,
    ):
        meta = cls._meta
        pk_fields = meta.pk_fields
        non_pks_non_generated = [
            f
            for f in meta.local_concrete_fields
            if f not in pk_fields and not f.generated
        ]

        if update_fields:
            non_pks_non_generated = [
                f
                for f in non_pks_non_generated
                if f.name in update_fields or f.attname in update_fields
            ]

        if not self._async_is_pk_set(meta) and not isinstance(
            getattr(self, meta.pk.attname), DatabaseDefault
        ):
            pk_val = meta.pk.get_pk_value_on_save(self)
            setattr(self, meta.pk.attname, pk_val)
        pk_set = self._async_is_pk_set(meta)
        if not pk_set and (force_update or update_fields):
            raise ValueError(
                "Cannot force an update in save() with no primary key."
            )
        updated = False
        # Skip an UPDATE when adding an instance and primary key has a default.
        if (
            not raw
            and not force_insert
            and not force_update
            and self._state.adding
            and all(
                f.has_default() or f.has_db_default() for f in meta.pk_fields
            )
        ):
            force_insert = True
        # If possible, try an UPDATE. If that doesn't update anything, do an
        # INSERT.
        if pk_set and not force_insert:
            base_qs = cls._async_base_manager.using(using)
            values = [
                (
                    f,
                    None,
                    (
                        getattr(self, f.attname)
                        if raw
                        else f.pre_save(self, False)
                    ),
                )
                for f in non_pks_non_generated
            ]
            forced_update = update_fields or force_update
            pk_val = self._async_get_pk_val(meta)
            returning_fields = [
                f
                for f in meta.local_concrete_fields
                if (
                    f.generated
                    and f.referenced_fields.intersection(non_pks_non_generated)
                )
            ]
            for field, _model, value in values:
                if (
                    update_fields is None or field.name in update_fields
                ) and hasattr(value, "resolve_expression"):
                    returning_fields.append(field)
            results = await self._async_do_update(
                base_qs,
                using,
                pk_val,
                values,
                update_fields,
                forced_update,
                returning_fields,
            )
            if updated := bool(results):
                self._async_assign_returned_values(
                    results[0], returning_fields
                )
            elif force_update:
                raise self.NotUpdated("Forced update did not affect any rows.")
            elif update_fields:
                raise self.NotUpdated(
                    "Save with update_fields did not affect any rows."
                )
        if not updated:
            if meta.order_with_respect_to:
                # If this is a model with an order_with_respect_to
                # autopopulate the _order field
                field = meta.order_with_respect_to
                filter_args = field.get_filter_kwargs_for_object(self)
                self._order = (
                    await cls._async_base_manager.using(using)
                    .filter(**filter_args)
                    .aaggregate(
                        _order__max=Coalesce(
                            ExpressionWrapper(
                                Max("_order") + Value(1),
                                output_field=IntegerField(),
                            ),
                            Value(0),
                        ),
                    )
                )["_order__max"]
            insert_fields = [
                f
                for f in meta.local_concrete_fields
                if not f.generated and (pk_set or f is not meta.auto_field)
            ]
            returning_fields = list(meta.db_returning_fields)
            can_return_columns_from_insert = async_connections[
                using
            ].features.can_return_columns_from_insert
            for field in insert_fields:
                value = (
                    getattr(self, field.attname)
                    if raw
                    else field.pre_save(self, add=True)
                )
                if hasattr(value, "resolve_expression"):
                    if field not in returning_fields:
                        returning_fields.append(field)
                elif (
                    field.db_returning
                    and not can_return_columns_from_insert
                    and not (pk_set and field is meta.auto_field)
                ):
                    returning_fields.remove(field)
            results = await self._async_do_insert(
                cls._async_base_manager,
                using,
                insert_fields,
                returning_fields,
                raw,
            )
            if results:
                self._async_assign_returned_values(
                    results[0], returning_fields
                )
        return updated

    async def _async_do_update(
        self,
        base_qs,
        using,
        pk_val,
        values,
        update_fields,
        forced_update,
        returning_fields,
    ):
        filtered = base_qs.filter(pk=pk_val)
        if not values:
            # We can end up here when saving a model in inheritance chain where
            # update_fields doesn't target any field in current model. In that
            # case we just say the update succeeded. Another case ending up
            # here is a model with just PK - in that case check that the PK
            # still exists.
            if update_fields is not None or await filtered.aexists():
                return [()]
            return []
        if self._meta.select_on_save and not forced_update:
            # It may happen that the object is deleted from the DB right after
            # this check, causing the subsequent UPDATE to return zero matching
            # rows. The same result can occur in some rare cases when the
            # database returns zero despite the UPDATE being executed
            # successfully (a row is matched and updated). In order to
            # distinguish these two cases, the object's existence in the
            # database is again checked for if the UPDATE query returns 0.
            if not await filtered.aexists():
                return []
            if results := await filtered._update(values, returning_fields):
                return results
            return [()] if await filtered.aexists() else []
        return await filtered._update(values, returning_fields)

    async def _async_do_insert(
        self, manager, using, fields, returning_fields, raw
    ):
        return await manager._insert(
            [self],
            fields=fields,
            returning_fields=returning_fields,
            using=using,
            raw=raw,
        )

    def _async_assign_returned_values(self, returned_values, returning_fields):
        returning_fields_iter = iter(returning_fields)
        for value, field in zip(returned_values, returning_fields_iter):
            setattr(self, field.attname, value)
        # Defer all fields that were meant to be updated with their database
        # resolved values but couldn't as they are effectively stale.
        for field in returning_fields_iter:
            self.__dict__.pop(field.attname, None)

    async def _async_prepare_related_fields_for_save(
        self, operation_name, fields=None
    ):
        # Ensure that a model instance without a PK hasn't been assigned to
        # a ForeignKey, GenericForeignKey or OneToOneField on this model. If
        # the field is nullable, allowing the save would result in silent data
        # loss.
        for field in self._meta.concrete_fields:
            if fields and field not in fields:
                continue
            # If the related field isn't cached, then an instance hasn't been
            # assigned and there's no need to worry about this check.
            if field.is_relation and field.is_cached(self):
                obj = getattr(self, field.name, None)
                if not obj:
                    continue
                # A pk may have been assigned manually to a model instance not
                # saved to the database (or auto-generated in a case like
                # UUIDField), but we allow the save to proceed and rely on the
                # database to raise an IntegrityError if applicable. If
                # constraints aren't supported by the database, there's the
                # unavoidable risk of data corruption.
                if not obj._async_is_pk_set():
                    # Remove the object from a related instance cache.
                    if not field.remote_field.multiple:
                        field.remote_field.delete_cached_value(obj)
                    raise ValueError(
                        "%s() prohibited to prevent data loss due to unsaved "
                        "related object '%s'." % (operation_name, field.name)
                    )
                elif getattr(self, field.attname) in field.empty_values:
                    # Set related object if it has been saved after an
                    # assignment.
                    setattr(self, field.name, obj)
                # If the relationship's pk/to_field was changed, clear the
                # cached relationship.
                if getattr(obj, field.target_field.attname) != getattr(
                    self, field.attname
                ):
                    field.delete_cached_value(self)
        # GenericForeignKeys are private.
        for field in self._meta.private_fields:
            if fields and field not in fields:
                continue
            if (
                field.is_relation
                and field.is_cached(self)
                and hasattr(field, "fk_field")
            ):
                obj = field.get_cached_value(self, default=None)
                if obj and not obj._async_is_pk_set():
                    raise ValueError(
                        f"{operation_name}() prohibited to prevent data loss due to "
                        f"unsaved related object '{field.name}'."
                    )

    async def async_delete(self, using=None, keep_parents=False):
        if not self._async_is_pk_set():
            raise ValueError(
                "%s object can't be deleted because its %s attribute is set "
                "to None." % (self._meta.object_name, self._meta.pk.attname)
            )
        using = using or router.db_for_write(self.__class__, instance=self)
        collector = Collector(using=using, origin=self)
        await collector.collect([self], keep_parents=keep_parents)
        return await collector.delete()

    async_delete.alters_data = True

    async def async_validate_unique(self, exclude=None):
        unique_checks, date_checks = self._get_unique_checks(exclude=exclude)

        errors = await self._async_perform_unique_checks(unique_checks)
        date_errors = await self._async_perform_date_checks(date_checks)

        for k, v in date_errors.items():
            errors.setdefault(k, []).extend(v)

        if errors:
            raise ValidationError(errors)

    async def _async_perform_unique_checks(self, unique_checks):
        errors = {}

        for model_class, unique_check in unique_checks:
            # Try to look up an existing object with the same values as this
            # object's values for all the unique field.

            lookup_kwargs = {}
            for field_name in unique_check:
                f = self._meta.get_field(field_name)
                lookup_value = getattr(self, f.attname)
                # TODO: Handle multiple backends with different feature flags.
                if lookup_value is None or (
                    lookup_value == ""
                    and connection.features.interprets_empty_strings_as_nulls
                ):
                    # no value, skip the lookup
                    continue
                if f in model_class._meta.pk_fields and not self._state.adding:
                    # no need to check for unique primary key when editing
                    continue
                lookup_kwargs[str(field_name)] = lookup_value

            # some fields were skipped, no reason to do the check
            if len(unique_check) != len(lookup_kwargs):
                continue

            qs = model_class.async_objects.filter(**lookup_kwargs)

            # Exclude the current object from the query if we are editing an
            # instance (as opposed to creating a new one)
            # Note that we need to use the pk as defined by model_class, not
            # self.pk. These can be different fields because model inheritance
            # allows single model to have effectively multiple primary keys.
            # Refs #17615.
            model_class_pk = self._async_get_pk_val(model_class._meta)
            if not self._state.adding and self._async_is_pk_set(
                model_class._meta
            ):
                qs = qs.exclude(pk=model_class_pk)
            if await qs.aexists():
                if len(unique_check) == 1:
                    key = unique_check[0]
                else:
                    key = NON_FIELD_ERRORS
                errors.setdefault(key, []).append(
                    self.unique_error_message(model_class, unique_check)
                )

        return errors

    async def _async_perform_date_checks(self, date_checks):
        errors = {}
        for model_class, lookup_type, field, unique_for in date_checks:
            lookup_kwargs = {}
            # there's a ticket to add a date lookup, we can remove this special
            # case if that makes its way in
            date = getattr(self, unique_for)
            if date is None:
                continue
            if lookup_type == "date":
                lookup_kwargs["%s__day" % unique_for] = date.day
                lookup_kwargs["%s__month" % unique_for] = date.month
                lookup_kwargs["%s__year" % unique_for] = date.year
            else:
                lookup_kwargs["%s__%s" % (unique_for, lookup_type)] = getattr(
                    date, lookup_type
                )
            lookup_kwargs[field] = getattr(self, field)

            qs = model_class.async_objects.filter(**lookup_kwargs)
            # Exclude the current object from the query if we are editing an
            # instance (as opposed to creating a new one)
            if not self._state.adding and self._async_is_pk_set():
                qs = qs.exclude(pk=self.pk)

            if await qs.aexists():
                errors.setdefault(field, []).append(
                    self.date_error_message(lookup_type, field, unique_for)
                )
        return errors

    async def async_validate_constraints(self, exclude=None):
        constraints = self.get_constraints()
        using = router.db_for_write(self.__class__, instance=self)

        errors = {}
        for model_class, model_constraints in constraints:
            for constraint in model_constraints:
                try:
                    await avalidate_constraint(
                        constraint,
                        model_class,
                        self,
                        exclude=exclude,
                        using=using,
                    )
                except ValidationError as e:
                    if (
                        getattr(e, "code", None) == "unique"
                        and len(constraint.fields) == 1
                    ):
                        errors.setdefault(constraint.fields[0], []).append(e)
                    else:
                        errors = e.update_error_dict(errors)
        if errors:
            raise ValidationError(errors)

    async def async_full_clean(
        self, exclude=None, validate_unique=True, validate_constraints=True
    ):
        errors = {}
        if exclude is None:
            exclude = set()
        else:
            exclude = set(exclude)

        try:
            await self.aclean_fields(exclude=exclude)
        except ValidationError as e:
            errors = e.update_error_dict(errors)

        # Form.clean() is run even if other validation fails, so do the
        # same with Model.clean() for consistency.
        try:
            await self._async_clean()
        except ValidationError as e:
            errors = e.update_error_dict(errors)

        # Run unique checks, but only for fields that passed validation.
        if validate_unique:
            for name in errors:
                if name != NON_FIELD_ERRORS and name not in exclude:
                    exclude.add(name)
            try:
                await self.async_validate_unique(exclude=exclude)
            except ValidationError as e:
                errors = e.update_error_dict(errors)

        # Run constraints checks, but only for fields that passed validation.
        if validate_constraints:
            for name in errors:
                if name != NON_FIELD_ERRORS and name not in exclude:
                    exclude.add(name)
            try:
                await self.async_validate_constraints(exclude=exclude)
            except ValidationError as e:
                errors = e.update_error_dict(errors)

        if errors:
            raise ValidationError(errors)
