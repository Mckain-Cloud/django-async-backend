"""Auth on the async connection.

Django's ``ModelBackend`` already has an async API, but it reaches the ORM
through the thread-backed ``objects.a*`` methods, on a *sync* connection.
``AsyncModelBackend`` performs the same queries with ``async_objects`` so a
login, ``request.auser()`` and permission checks run on the request's async
connection and inside its transaction::

    AUTHENTICATION_BACKENDS = ["django_async_backend.contrib.auth.AsyncModelBackend"]

``alogin()`` sends ``user_logged_in``, whose default receiver updates
``last_login`` with a sync ``save()``. ``patch_last_login()`` swaps that
receiver for an async one that uses ``async_save()``; it is applied on
startup when ``ASYNC_BACKEND_ASYNC_LAST_LOGIN = True``.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.models import Permission
from django.utils import timezone


class AsyncModelBackend(ModelBackend):
    """``ModelBackend`` whose async methods use the async ORM."""

    async def aauthenticate(
        self, request, username=None, password=None, **kwargs
    ):
        from django.contrib.auth import (
            acheck_password_with_timing_attack_mitigation,
        )

        UserModel = get_user_model()
        if username is None:
            username = kwargs.get(UserModel.USERNAME_FIELD)
        if username is None or password is None:
            return
        try:
            user = await UserModel.async_objects.aget(
                **{UserModel.USERNAME_FIELD: username}
            )
        except UserModel.DoesNotExist:
            user = None
        if await acheck_password_with_timing_attack_mitigation(
            user, password
        ) and self.user_can_authenticate(user):
            return user

    async def aget_user(self, user_id):
        UserModel = get_user_model()
        try:
            user = await UserModel.async_objects.aget(pk=user_id)
        except UserModel.DoesNotExist:
            return None
        return user if self.user_can_authenticate(user) else None

    def _aget_user_permissions_queryset(self, user_obj):
        return Permission.async_objects.filter(user=user_obj)

    def _aget_group_permissions_queryset(self, user_obj):
        return Permission.async_objects.filter(group__user=user_obj)

    async def _aget_permissions(self, user_obj, obj, from_name):
        if not user_obj.is_active or user_obj.is_anonymous or obj is not None:
            return set()

        perm_cache_name = "_%s_perm_cache" % from_name
        if not hasattr(user_obj, perm_cache_name):
            if user_obj.is_superuser:
                perms = Permission.async_objects.all()
            else:
                perms = getattr(
                    self, "_aget_%s_permissions_queryset" % from_name
                )(user_obj)
            perms = perms.values_list(
                "content_type__app_label", "codename"
            ).order_by()
            setattr(
                user_obj,
                perm_cache_name,
                {"%s.%s" % (ct, name) async for ct, name in perms},
            )
        return getattr(user_obj, perm_cache_name)

    async def awith_perm(
        self, perm, is_active=True, include_superusers=True, obj=None
    ):
        # Django returns a sync queryset here; return the async one.
        queryset = super().with_perm(
            perm,
            is_active=is_active,
            include_superusers=include_superusers,
            obj=obj,
        )
        UserModel = get_user_model()
        return UserModel.async_objects.filter(pk__in=queryset.values("pk"))


async def awarm_permissions(user):
    """Load the user's permissions on the async connection.

    Django's sync ``has_perm()``/``has_perms()``/``has_module_perms()`` then
    find the per-user caches and run no query, so sync permission checks
    (admin, DRF ``DjangoModelPermissions``, the ``perms`` template variable)
    stay on this task's connection. Superusers short-circuit without a query.
    """
    if user is None or user.is_anonymous or getattr(user, "is_superuser", False):
        return
    if hasattr(user, "aget_all_permissions"):
        await user.aget_all_permissions()


async def aupdate_last_login(sender, user, **kwargs):
    """Async replacement for `django.contrib.auth.models.update_last_login`.

    Runs in the task that called `alogin()` (see
    `django_async_backend.dispatch`), on its connection and inside its
    transaction, exactly like Django's sync receiver.
    """
    user.last_login = timezone.now()
    await user.async_save(update_fields=["last_login"])


def patch_last_login():
    """Replace the sync ``update_last_login`` receiver with the async one."""
    from django.contrib.auth.models import update_last_login
    from django.contrib.auth.signals import user_logged_in

    user_logged_in.disconnect(
        update_last_login, dispatch_uid="update_last_login"
    )
    user_logged_in.connect(
        aupdate_last_login, dispatch_uid="async_update_last_login"
    )
