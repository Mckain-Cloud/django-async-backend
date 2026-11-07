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


def patch_user_async_save():
    """Give ``AbstractBaseUser`` the async twin of its ``save()`` override.

    Django's ``AbstractBaseUser.save()`` fires
    ``password_validation.password_changed()`` and clears the plain-text
    password after saving. The generated ``async_save()`` comes from
    ``Model.save`` and would skip that; mirror it here so the override check
    (``async_backend.W001``) is satisfied for the user model.
    """
    from django.contrib.auth import password_validation
    from django.contrib.auth.base_user import AbstractBaseUser

    if "async_save" in vars(AbstractBaseUser):
        return

    async def async_save(self, *args, **kwargs):
        await super(AbstractBaseUser, self).async_save(*args, **kwargs)
        if self._password is not None:
            password_validation.password_changed(self._password, self)
            self._password = None

    async_save.__qualname__ = "AbstractBaseUser.async_save"
    AbstractBaseUser.async_save = async_save

    # AbstractUser.clean() normalises the e-mail address: no query, so the
    # async twin can simply call it.
    from django.contrib.auth.models import AbstractUser

    if "aclean" not in vars(AbstractUser):

        async def aclean(self):
            return self.clean()

        aclean.__qualname__ = "AbstractUser.aclean"
        AbstractUser.aclean = aclean


def patch_token_async_save():
    """Async twins for ``rest_framework.authtoken`` model overrides.

    ``Token.save()`` generates the key; ``TokenProxy.delete()`` deletes
    through the concrete ``Token`` because the proxy maps ``pk`` to the user
    id. No-op when the app is not installed.
    """
    try:
        from rest_framework.authtoken.models import Token, TokenProxy
    except Exception:  # pragma: no cover - app not installed / importable
        return

    if "async_save" not in vars(Token):

        async def async_save(self, *args, **kwargs):
            if not self.key:
                self.key = self.generate_key()
            return await super(Token, self).async_save(*args, **kwargs)

        async_save.__qualname__ = "Token.async_save"
        Token.async_save = async_save

    if "async_delete" not in vars(TokenProxy):

        async def async_delete(self, using=None, keep_parents=False):
            from django.db import router

            if self.key is None:
                raise ValueError(
                    "%s object can't be deleted because its %s attribute is "
                    "set to None." % (self._meta.object_name, self._meta.pk.attname)
                )
            using = using or router.db_for_write(self.__class__, instance=self)
            deleted = await Token.async_objects.using(using).filter(key=self.key).adelete()
            self.key = None
            return deleted

        async_delete.__qualname__ = "TokenProxy.async_delete"
        TokenProxy.async_delete = async_delete


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
