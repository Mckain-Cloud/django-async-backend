"""Async related managers.

Django builds reverse-FK and many-to-many managers with two factories in
``django.db.models.fields.related_descriptors``. Both take the manager class to
subclass as a parameter, so handing them ``AsyncManager`` yields managers whose
``get_queryset()`` returns an *async* queryset with the relation filters
applied. Reads come for free that way.

The write methods (``add``/``remove``/``clear``/``set``/``create`` and their
``a``-prefixed thread-hop twins) are reimplemented here on the async ORM, so
they run on the task's connection and inside ``async_atomic()``.

They are exposed as ``instance.<relation>.async_objects``::

    async for book in author.books.async_objects.all(): ...
    await author.books.async_objects.aadd(book)
    await book.tags.async_objects.aset([t1, t2])
"""

from django.db import router
from django.db.models import Q, signals
from django.db.models.fields import related_descriptors
from django.db.models.utils import resolve_callables

from django_async_backend.db.models.manager import AsyncManager
from django_async_backend.db.transaction import async_atomic

_REVERSE_CACHE = {}
_M2M_CACHE = {}


def create_async_reverse_many_to_one_manager(rel):
    """Async counterpart of ``create_reverse_many_to_one_manager()``."""
    try:
        return _REVERSE_CACHE[rel]
    except KeyError:
        pass

    base = related_descriptors.create_reverse_many_to_one_manager(
        AsyncManager, rel
    )

    class AsyncRelatedManager(base):
        def __call__(self, *, manager):
            raise TypeError(
                "async_objects does not support selecting another manager; "
                "use the model's async_objects directly."
            )

        def _check_and_update_obj(self, obj):
            if not isinstance(obj, self.model):
                raise TypeError(
                    "'%s' instance expected, got %r"
                    % (self.model._meta.object_name, obj)
                )
            setattr(obj, self.field.name, self.instance)

        async def aadd(self, *objs, bulk=True):
            self._check_fk_val()
            self._remove_prefetched_objects()
            db = router.db_for_write(self.model, instance=self.instance)

            if bulk:
                pks = []
                for obj in objs:
                    self._check_and_update_obj(obj)
                    if obj._state.adding or obj._state.db != db:
                        raise ValueError(
                            "%r instance isn't saved. Use bulk=False or save "
                            "the object first." % obj
                        )
                    pks.append(obj.pk)
                await self.model._async_base_manager.using(db).filter(
                    pk__in=pks
                ).aupdate(**{self.field.name: self.instance})
            else:
                async with async_atomic(using=db, savepoint=False):
                    for obj in objs:
                        self._check_and_update_obj(obj)
                        await obj.async_save()

        aadd.alters_data = True

        async def acreate(self, **kwargs):
            self._check_fk_val()
            self._remove_prefetched_objects()
            kwargs[self.field.name] = self.instance
            db = router.db_for_write(self.model, instance=self.instance)
            return await super(base, self.db_manager(db)).acreate(**kwargs)

        acreate.alters_data = True

        async def aget_or_create(self, **kwargs):
            self._check_fk_val()
            kwargs[self.field.name] = self.instance
            db = router.db_for_write(self.model, instance=self.instance)
            return await super(base, self.db_manager(db)).aget_or_create(
                **kwargs
            )

        aget_or_create.alters_data = True

        async def aupdate_or_create(self, **kwargs):
            self._check_fk_val()
            kwargs[self.field.name] = self.instance
            db = router.db_for_write(self.model, instance=self.instance)
            return await super(base, self.db_manager(db)).aupdate_or_create(
                **kwargs
            )

        aupdate_or_create.alters_data = True

        if rel.field.null:

            async def aremove(self, *objs, bulk=True):
                if not objs:
                    return
                self._check_fk_val()
                val = self.field.get_foreign_related_value(self.instance)
                old_ids = set()
                for obj in objs:
                    if not isinstance(obj, self.model):
                        raise TypeError(
                            "'%s' instance expected, got %r"
                            % (self.model._meta.object_name, obj)
                        )
                    if self.field.get_local_related_value(obj) == val:
                        old_ids.add(obj.pk)
                    else:
                        raise self.field.remote_field.model.DoesNotExist(
                            "%r is not related to %r." % (obj, self.instance)
                        )
                await self._aclear(self.filter(pk__in=old_ids), bulk)

            aremove.alters_data = True

            async def aclear(self, *, bulk=True):
                self._check_fk_val()
                await self._aclear(self, bulk)

            aclear.alters_data = True

            async def _aclear(self, queryset, bulk):
                self._remove_prefetched_objects()
                db = router.db_for_write(self.model, instance=self.instance)
                queryset = queryset.using(db)
                if bulk:
                    await queryset.aupdate(**{self.field.name: None})
                else:
                    async with async_atomic(using=db, savepoint=False):
                        async for obj in queryset:
                            setattr(obj, self.field.name, None)
                            await obj.async_save(
                                update_fields=[self.field.name]
                            )

            _aclear.alters_data = True

        async def aset(self, objs, *, bulk=True, clear=False):
            self._check_fk_val()
            objs = tuple(objs)

            if self.field.null:
                db = router.db_for_write(self.model, instance=self.instance)
                async with async_atomic(using=db, savepoint=False):
                    if clear:
                        await self.aclear(bulk=bulk)
                        await self.aadd(*objs, bulk=bulk)
                    else:
                        old_objs = {o async for o in self.using(db).all()}
                        new_objs = []
                        for obj in objs:
                            if obj in old_objs:
                                old_objs.remove(obj)
                            else:
                                new_objs.append(obj)
                        await self.aremove(*old_objs, bulk=bulk)
                        await self.aadd(*new_objs, bulk=bulk)
            else:
                await self.aadd(*objs, bulk=bulk)

        aset.alters_data = True

    AsyncRelatedManager.__name__ = "AsyncRelatedManager"
    _REVERSE_CACHE[rel] = AsyncRelatedManager
    return AsyncRelatedManager


def create_async_many_to_many_manager(rel, reverse):
    """Async counterpart of ``create_forward_many_to_many_manager()``."""
    key = (rel, reverse)
    try:
        return _M2M_CACHE[key]
    except KeyError:
        pass

    base = related_descriptors.create_forward_many_to_many_manager(
        AsyncManager, rel, reverse
    )

    class AsyncManyRelatedManager(base):
        def __call__(self, *, manager):
            raise TypeError(
                "async_objects does not support selecting another manager; "
                "use the model's async_objects directly."
            )

        @property
        def _through_manager(self):
            # The through model may be auto-created; the runtime patch gives
            # every model an async manager.
            return self.through.async_objects

        def _build_remove_filters(self, removed_vals):
            # Same as Django's, but recognise the *async* QuerySet class.
            from django_async_backend.db.models.query import QuerySet

            filters = Q.create([(self.source_field_name, self.related_val)])
            removed_vals_filters = (
                not isinstance(removed_vals, QuerySet)
                or removed_vals._has_filters()
            )
            if removed_vals_filters:
                filters &= Q.create(
                    [(f"{self.target_field_name}__in", removed_vals)]
                )
            if self.symmetrical:
                symmetrical_filters = Q.create(
                    [(self.target_field_name, self.related_val)]
                )
                if removed_vals_filters:
                    symmetrical_filters &= Q.create(
                        [(f"{self.source_field_name}__in", removed_vals)]
                    )
                filters |= symmetrical_filters
            return filters

        # ---- add -------------------------------------------------------

        async def aadd(self, *objs, through_defaults=None):
            self._remove_prefetched_objects()
            db = router.db_for_write(self.through, instance=self.instance)
            await self._aadd_base(
                *objs, through_defaults=through_defaults, using=db
            )

        aadd.alters_data = True

        async def _aadd_base(
            self, *objs, through_defaults=None, using=None, raw=False
        ):
            db = using or router.db_for_write(
                self.through, instance=self.instance
            )
            async with async_atomic(using=db, savepoint=False):
                await self._aadd_items(
                    self.source_field_name,
                    self.target_field_name,
                    *objs,
                    through_defaults=through_defaults,
                    using=db,
                    raw=raw,
                )
                if self.symmetrical:
                    await self._aadd_items(
                        self.target_field_name,
                        self.source_field_name,
                        *objs,
                        through_defaults=through_defaults,
                        using=db,
                        raw=raw,
                    )

        async def _aget_missing_target_ids(
            self, source_field_name, target_field_name, db, target_ids
        ):
            vals = {
                v
                async for v in self._through_manager.using(db)
                .values_list(target_field_name, flat=True)
                .filter(
                    **{
                        source_field_name: self.related_val[0],
                        "%s__in" % target_field_name: target_ids,
                    }
                )
            }
            return target_ids.difference(vals)

        async def _aadd_items(
            self,
            source_field_name,
            target_field_name,
            *objs,
            through_defaults=None,
            using=None,
            raw=False,
        ):
            if not objs:
                return
            through_defaults = dict(resolve_callables(through_defaults or {}))
            target_ids = self._get_target_ids(target_field_name, objs)
            db = using or router.db_for_write(
                self.through, instance=self.instance
            )
            (
                can_ignore_conflicts,
                must_send_signals,
                can_fast_add,
            ) = self._get_add_plan(db, source_field_name)
            if can_fast_add:
                await self._through_manager.using(db).abulk_create(
                    [
                        self.through(
                            **{
                                "%s_id" % source_field_name: self.related_val[
                                    0
                                ],
                                "%s_id" % target_field_name: target_id,
                            }
                        )
                        for target_id in target_ids
                    ],
                    ignore_conflicts=True,
                )
                return

            missing_target_ids = await self._aget_missing_target_ids(
                source_field_name, target_field_name, db, target_ids
            )
            async with async_atomic(using=db, savepoint=False):
                if must_send_signals:
                    await signals.m2m_changed.asend(
                        sender=self.through,
                        action="pre_add",
                        instance=self.instance,
                        reverse=self.reverse,
                        model=self.model,
                        pk_set=missing_target_ids,
                        using=db,
                        raw=raw,
                    )
                await self._through_manager.using(db).abulk_create(
                    [
                        self.through(
                            **through_defaults,
                            **{
                                "%s_id" % source_field_name: self.related_val[
                                    0
                                ],
                                "%s_id" % target_field_name: target_id,
                            },
                        )
                        for target_id in missing_target_ids
                    ],
                    ignore_conflicts=can_ignore_conflicts,
                )
                if must_send_signals:
                    await signals.m2m_changed.asend(
                        sender=self.through,
                        action="post_add",
                        instance=self.instance,
                        reverse=self.reverse,
                        model=self.model,
                        pk_set=missing_target_ids,
                        using=db,
                        raw=raw,
                    )

        # ---- remove ----------------------------------------------------

        async def aremove(self, *objs):
            self._remove_prefetched_objects()
            db = router.db_for_write(self.through, instance=self.instance)
            await self._aremove_base(*objs, using=db)

        aremove.alters_data = True

        async def _aremove_base(self, *objs, using=None, raw=False):
            db = using or router.db_for_write(
                self.through, instance=self.instance
            )
            await self._aremove_items(
                self.source_field_name,
                self.target_field_name,
                *objs,
                using=db,
                raw=raw,
            )

        async def _aremove_items(
            self, source_field_name, target_field_name, *objs, using=None,
            raw=False,
        ):
            if not objs:
                return
            old_ids = set()
            for obj in objs:
                if isinstance(obj, self.model):
                    fk_val = self.target_field.get_foreign_related_value(obj)[
                        0
                    ]
                    old_ids.add(fk_val)
                else:
                    old_ids.add(obj)
            db = using or router.db_for_write(
                self.through, instance=self.instance
            )
            async with async_atomic(using=db, savepoint=False):
                await signals.m2m_changed.asend(
                    sender=self.through,
                    action="pre_remove",
                    instance=self.instance,
                    reverse=self.reverse,
                    model=self.model,
                    pk_set=old_ids,
                    using=db,
                    raw=raw,
                )
                target_model_qs = super(base, self).get_queryset()
                if target_model_qs._has_filters():
                    old_vals = target_model_qs.using(db).filter(
                        **{
                            "%s__in"
                            % self.target_field.target_field.attname: old_ids
                        }
                    )
                else:
                    old_vals = old_ids
                filters = self._build_remove_filters(old_vals)
                await self._through_manager.using(db).filter(filters).adelete()
                await signals.m2m_changed.asend(
                    sender=self.through,
                    action="post_remove",
                    instance=self.instance,
                    reverse=self.reverse,
                    model=self.model,
                    pk_set=old_ids,
                    using=db,
                    raw=raw,
                )

        # ---- clear / set -----------------------------------------------

        async def aclear(self):
            self._remove_prefetched_objects()
            db = router.db_for_write(self.through, instance=self.instance)
            await self._aclear_base(using=db)

        aclear.alters_data = True

        async def _aclear_base(self, using=None, raw=False):
            db = using or router.db_for_write(
                self.through, instance=self.instance
            )
            async with async_atomic(using=db, savepoint=False):
                await signals.m2m_changed.asend(
                    sender=self.through,
                    action="pre_clear",
                    instance=self.instance,
                    reverse=self.reverse,
                    model=self.model,
                    pk_set=None,
                    using=db,
                    raw=raw,
                )
                filters = self._build_remove_filters(
                    super(base, self).get_queryset().using(db)
                )
                await self._through_manager.using(db).filter(filters).adelete()
                await signals.m2m_changed.asend(
                    sender=self.through,
                    action="post_clear",
                    instance=self.instance,
                    reverse=self.reverse,
                    model=self.model,
                    pk_set=None,
                    using=db,
                    raw=raw,
                )

        async def aset(self, objs, *, clear=False, through_defaults=None):
            objs = tuple(objs)
            db = router.db_for_write(self.through, instance=self.instance)
            async with async_atomic(using=db, savepoint=False):
                self._remove_prefetched_objects()
                if clear:
                    await self._aclear_base(using=db)
                    await self._aadd_base(
                        *objs, through_defaults=through_defaults, using=db
                    )
                else:
                    old_ids = {
                        v
                        async for v in self.using(db).values_list(
                            self.target_field.target_field.attname, flat=True
                        )
                    }
                    new_objs = []
                    for obj in objs:
                        fk_val = (
                            self.target_field.get_foreign_related_value(obj)[0]
                            if isinstance(obj, self.model)
                            else self.target_field.get_prep_value(obj)
                        )
                        if fk_val in old_ids:
                            old_ids.remove(fk_val)
                        else:
                            new_objs.append(obj)
                    await self._aremove_base(*old_ids, using=db)
                    await self._aadd_base(
                        *new_objs, through_defaults=through_defaults, using=db
                    )

        aset.alters_data = True

        # ---- create ----------------------------------------------------

        async def acreate(self, *, through_defaults=None, **kwargs):
            db = router.db_for_write(
                self.instance.__class__, instance=self.instance
            )
            new_obj = await super(base, self.db_manager(db)).acreate(**kwargs)
            await self.aadd(new_obj, through_defaults=through_defaults)
            return new_obj

        acreate.alters_data = True

        async def aget_or_create(self, *, through_defaults=None, **kwargs):
            db = router.db_for_write(
                self.instance.__class__, instance=self.instance
            )
            obj, created = await super(
                base, self.db_manager(db)
            ).aget_or_create(**kwargs)
            if created:
                await self.aadd(obj, through_defaults=through_defaults)
            return obj, created

        aget_or_create.alters_data = True

        async def aupdate_or_create(self, *, through_defaults=None, **kwargs):
            db = router.db_for_write(
                self.instance.__class__, instance=self.instance
            )
            obj, created = await super(
                base, self.db_manager(db)
            ).aupdate_or_create(**kwargs)
            if created:
                await self.aadd(obj, through_defaults=through_defaults)
            return obj, created

        aupdate_or_create.alters_data = True

    AsyncManyRelatedManager.__name__ = "AsyncManyRelatedManager"
    _M2M_CACHE[key] = AsyncManyRelatedManager
    return AsyncManyRelatedManager


def _async_objects_for_reverse(rel):
    def async_objects(self):
        manager = create_async_reverse_many_to_one_manager(rel)(self.instance)
        manager._db = self._db
        return manager

    return property(async_objects)


def _async_objects_for_m2m(rel, reverse):
    def async_objects(self):
        manager = create_async_many_to_many_manager(rel, reverse)(
            instance=self.instance
        )
        manager._db = self._db
        return manager

    return property(async_objects)


_patched = False


def patch_related_managers():
    """Give every Django related manager an ``async_objects`` property.

    Django builds the manager classes lazily through two module-level
    factories; wrapping those is enough to reach every relation, including
    ones declared on third-party models.
    """
    global _patched
    if _patched:  # pragma: no cover
        return
    _patched = True

    original_reverse = related_descriptors.create_reverse_many_to_one_manager
    original_m2m = related_descriptors.create_forward_many_to_many_manager

    def reverse_factory(superclass, rel):
        cls = original_reverse(superclass, rel)
        if not issubclass(superclass, AsyncManager):
            cls.async_objects = _async_objects_for_reverse(rel)
        return cls

    def m2m_factory(superclass, rel, reverse):
        cls = original_m2m(superclass, rel, reverse)
        if not issubclass(superclass, AsyncManager):
            cls.async_objects = _async_objects_for_m2m(rel, reverse)
        return cls

    related_descriptors.create_reverse_many_to_one_manager = reverse_factory
    related_descriptors.create_forward_many_to_many_manager = m2m_factory
