"""Async permission checks for the admin.

Django's ``ModelAdmin`` consults ``has_add_permission()``,
``has_change_permission()``, ``has_view_permission()``,
``has_delete_permission()`` and ``has_module_permission()`` from dozens of
sync call sites (forms, inlines, responses, templates, the index). A
permission backend that queries per object (``has_perm(user, perm, obj)``)
therefore raised ``SynchronousOnlyOperation`` in the async admin.

``AsyncPermissionsMixin`` adds ``ahas_*_permission()`` twins, which use the
backends' async API (``user.ahas_perm()``, Django 5.2+), and makes the sync
methods read a per-request cache that the async views fill ahead of time
with ``aprepare_permissions(request, obj)`` for every object they will ask
about. A miss falls back to Django's sync implementation, which is fine for
backends without per-object queries and loud for the others.

Override the ``ahas_*`` methods to customise permissions in an async admin;
the sync ones keep working for Django's callers.
"""

from django.contrib.auth import get_permission_codename

_CACHE_ATTR = "_async_admin_permissions"
ACTIONS = ("add", "change", "delete", "view")


def _cache(request):
    try:
        return request.__dict__[_CACHE_ATTR]
    except KeyError:
        cache = request.__dict__[_CACHE_ATTR] = {}
        return cache


def _obj_key(obj):
    return None if obj is None else obj.pk


class AsyncPermissionsMixin:
    # ---- async API (override these) -----------------------------------

    def _perm_opts_for(self, action):
        """The model whose permission the action maps to.

        Auto-created many-to-many through models use the target model's
        permissions, as ``InlineModelAdmin._has_any_perms_for_target_model``
        does.
        """
        opts = self.opts
        if getattr(opts, "auto_created", False) and getattr(
            self, "parent_model", None
        ):
            for field in opts.fields:
                if (
                    field.remote_field
                    and field.remote_field.model != self.parent_model
                ):
                    return field.remote_field.model._meta
        return opts

    async def _ahas_perm(self, request, action, obj=None):
        """Model-level permission, or the object-level one for ``obj``.

        Django's ``ModelAdmin`` only ever checks the model-level permission
        and ignores ``obj``; ``ModelBackend`` answers ``False`` for any
        object. Also asking for ``obj`` is what lets an object-permission
        backend grant access to individual rows.
        """
        opts = self._perm_opts_for(action)
        perm = "%s.%s" % (
            opts.app_label,
            get_permission_codename(action, opts),
        )
        if await request.user.ahas_perm(perm):
            return True
        if obj is not None:
            return await request.user.ahas_perm(perm, obj)
        return False

    async def ahas_add_permission(self, request, obj=None):
        if getattr(self.opts, "auto_created", False):
            return await self._ahas_perm(request, "change", obj)
        return await self._ahas_perm(request, "add")

    async def ahas_change_permission(self, request, obj=None):
        return await self._ahas_perm(request, "change", obj)

    async def ahas_delete_permission(self, request, obj=None):
        if getattr(self.opts, "auto_created", False):
            return await self._ahas_perm(request, "change", obj)
        return await self._ahas_perm(request, "delete", obj)

    async def ahas_view_permission(self, request, obj=None):
        if getattr(self.opts, "auto_created", False):
            return await self._ahas_perm(
                request, "view", obj
            ) or await self._ahas_perm(request, "change", obj)
        return await self._ahas_perm(
            request, "view", obj
        ) or await self._ahas_perm(request, "change", obj)

    async def ahas_module_permission(self, request):
        return await request.user.ahas_module_perms(self.opts.app_label)

    # ---- preparation ----------------------------------------------------

    def _cache_key(self):
        # Inline admins are instantiated afresh per call, so key by class.
        return type(self)

    async def aprepare_permissions(self, request, obj=None, actions=ACTIONS):
        """Compute and cache the checks the sync callers will make."""
        cache = _cache(request)
        key = self._cache_key()
        if (key, "module", None) not in cache:
            cache[key, "module", None] = await self.ahas_module_permission(
                request
            )
        for action in actions:
            k = (key, action, _obj_key(obj))
            if k not in cache:
                method = getattr(self, "ahas_%s_permission" % action)
                cache[k] = await method(request, obj)
            if obj is not None and action != "add":
                # The model-level answer is asked for too (menus, buttons).
                k = (key, action, None)
                if k not in cache:
                    cache[k] = await method(request, None)
        return cache

    def _cached_permission(self, request, action, obj, fallback):
        cache = request.__dict__.get(_CACHE_ATTR)
        if cache is not None:
            try:
                return cache[self._cache_key(), action, _obj_key(obj)]
            except KeyError:
                pass
        return fallback()

    # ---- sync API Django calls ----------------------------------------

    def has_add_permission(self, request, obj=None):
        return self._cached_permission(
            request,
            "add",
            None,
            lambda: self._sync_fallback("has_add_permission", request, obj),
        )

    def has_change_permission(self, request, obj=None):
        return self._cached_permission(
            request,
            "change",
            obj,
            lambda: self._sync_fallback("has_change_permission", request, obj),
        )

    def has_delete_permission(self, request, obj=None):
        return self._cached_permission(
            request,
            "delete",
            obj,
            lambda: self._sync_fallback("has_delete_permission", request, obj),
        )

    def has_view_permission(self, request, obj=None):
        return self._cached_permission(
            request,
            "view",
            obj,
            lambda: self._sync_fallback("has_view_permission", request, obj),
        )

    def has_module_permission(self, request):
        return self._cached_permission(
            request,
            "module",
            None,
            lambda: super(AsyncPermissionsMixin, self).has_module_permission(
                request
            ),
        )

    def _sync_fallback(self, name, request, obj):
        method = getattr(super(AsyncPermissionsMixin, self), name)
        if name == "has_add_permission":
            # ModelAdmin.has_add_permission(request); inlines take obj.
            try:
                return method(request, obj)
            except TypeError:
                return method(request)
        return method(request, obj)
