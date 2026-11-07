import warnings

from django.apps import AppConfig
from django.conf import settings

MIDDLEWARE_PATH = "django_async_backend.middleware.close_async_connections"


def check_middleware():
    if getattr(settings, "ASYNC_BACKEND_DISABLE_MIDDLEWARE_CHECK", False):
        return

    if MIDDLEWARE_PATH not in settings.MIDDLEWARE:
        warnings.warn(
            f"Add '{MIDDLEWARE_PATH}' to MIDDLEWARE. Under ASGI it releases "
            "async connections at the end of each request; without it every "
            "request leaks its connection. If you release them another way, "
            "set ASYNC_BACKEND_DISABLE_MIDDLEWARE_CHECK = True.",
            stacklevel=2,
        )


class DjangoAsyncBackendConfig(AppConfig):
    name = "django_async_backend"
    verbose_name = "Django async backend"

    def ready(self):
        from django_async_backend.db.models.patch import _patch_model

        _patch_model()
        check_middleware()

        from django_async_backend import checks

        checks.register()

        if getattr(
            settings, "ASYNC_BACKEND_SEQUENTIAL_SIGNAL_RECEIVERS", True
        ):
            from django_async_backend.dispatch import patch_signal_dispatch

            patch_signal_dispatch()

        if getattr(settings, "ASYNC_BACKEND_ASYNC_TEMPLATES", True):
            from django_async_backend.template.patch import (
                patch_template_resolve,
            )

            patch_template_resolve()

        from django_async_backend.contrib.auth import (
            patch_token_async_save,
            patch_user_async_save,
        )

        patch_user_async_save()
        patch_token_async_save()

        if getattr(settings, "ASYNC_BACKEND_ASYNC_LAST_LOGIN", False):
            from django_async_backend.contrib.auth import patch_last_login

            patch_last_login()

        from django_async_backend import hops

        hops.install(getattr(settings, "ASYNC_BACKEND_SYNC_HOPS", "off"))
