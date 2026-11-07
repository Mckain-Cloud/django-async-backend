"""System checks for code the async paths cannot reach.

``async_backend.W001`` – a model overrides ``save()``, ``delete()`` or
``clean()`` without the async twin (``async_save()``, ``async_delete()``,
``aclean()``). The async methods are generated from Django's bodies and do
not call a sync override, so the override is silently skipped on the async
path. Port the logic to the async method (and have the sync one delegate,
or keep both in step).

``async_backend.W002`` – a sync receiver is connected to a request-time
signal. ``Signal.asend()`` runs it through ``sync_to_async()`` on the sync
connection: outside the sender's transaction, so it cannot see the row it
was told about. Make it ``async def`` and it runs in the sender's task.

Set ``ASYNC_BACKEND_OVERRIDE_CHECK`` / ``ASYNC_BACKEND_SYNC_RECEIVER_CHECK``
to ``"error"`` to turn them into errors, or ``"off"`` to silence them.
"""

import inspect
import weakref

from django.apps import apps
from django.conf import settings
from django.core import checks
from django.db.models import Model
from django.db.models import signals as model_signals

OVERRIDE_PAIRS = (
    ("save", "async_save"),
    ("delete", "async_delete"),
    ("clean", "aclean"),
)

REQUEST_TIME_SIGNALS = [
    ("django.db.models.signals.pre_save", model_signals.pre_save),
    ("django.db.models.signals.post_save", model_signals.post_save),
    ("django.db.models.signals.pre_delete", model_signals.pre_delete),
    ("django.db.models.signals.post_delete", model_signals.post_delete),
    ("django.db.models.signals.m2m_changed", model_signals.m2m_changed),
]


def _level(setting, default="warn"):
    value = getattr(settings, setting, default)
    return {"warn": checks.Warning, "error": checks.Error}.get(value)


def _base_functions(name):
    """The implementations of ``name`` that count as 'not overridden'."""
    from django_async_backend.db.models.base import AsyncModelMixin

    bases = set()
    for cls in (Model, AsyncModelMixin):
        try:
            bases.add(inspect.getattr_static(cls, name))
        except AttributeError:
            pass
    return bases


def check_model_overrides(app_configs=None, **kwargs):
    klass = _level("ASYNC_BACKEND_OVERRIDE_CHECK")
    if klass is None:
        return []
    models = (
        apps.get_models()
        if app_configs is None
        else [m for cfg in app_configs for m in cfg.get_models()]
    )
    errors = []
    for model in models:
        for sync_name, async_name in OVERRIDE_PAIRS:
            try:
                sync_impl = inspect.getattr_static(model, sync_name)
            except AttributeError:
                continue
            if sync_impl in _base_functions(sync_name):
                continue
            try:
                async_impl = inspect.getattr_static(model, async_name)
            except AttributeError:
                async_impl = None
            if async_impl is not None and async_impl not in _base_functions(async_name):
                continue
            errors.append(
                klass(
                    "%s overrides %s() but not %s()." % (
                        model._meta.label, sync_name, async_name
                    ),
                    hint=(
                        "%s() is generated from Django's implementation and "
                        "does not call the sync override, so the async path "
                        "skips it. Define async def %s() with the same logic. "
                        "Silence with ASYNC_BACKEND_OVERRIDE_CHECK = 'off'."
                        % (async_name, async_name)
                    ),
                    obj=model,
                    id="async_backend.W001",
                )
            )
    return errors


def _iter_sync_receivers(signal):
    for entry in signal.receivers:
        # Django 6: (lookup_key, receiver, sender_ref, is_async);
        # Django 5: (lookup_key, receiver, is_async).
        receiver = entry[1]
        is_async = entry[-1] if len(entry) > 2 else False
        if is_async:
            continue
        if isinstance(receiver, weakref.ReferenceType):
            receiver = receiver()
        if receiver is None:
            continue
        yield receiver


def _describe(receiver):
    module = getattr(receiver, "__module__", None) or "?"
    name = getattr(receiver, "__qualname__", None) or repr(receiver)
    return "%s.%s" % (module, name)


def check_sync_receivers(app_configs=None, **kwargs):
    klass = _level("ASYNC_BACKEND_SYNC_RECEIVER_CHECK")
    if klass is None:
        return []
    signals = list(REQUEST_TIME_SIGNALS)
    try:
        from django.contrib.auth import signals as auth_signals

        signals += [
            ("django.contrib.auth.signals.user_logged_in", auth_signals.user_logged_in),
            ("django.contrib.auth.signals.user_logged_out", auth_signals.user_logged_out),
            ("django.contrib.auth.signals.user_login_failed", auth_signals.user_login_failed),
        ]
    except ImportError:  # pragma: no cover
        pass
    errors = []
    for label, signal in signals:
        names = sorted({_describe(r) for r in _iter_sync_receivers(signal)})
        if not names:
            continue
        errors.append(
            klass(
                "Sync receivers connected to %s: %s." % (label, ", ".join(names)),
                hint=(
                    "Under asend() a sync receiver runs in a worker thread on "
                    "the sync connection, outside the sender's transaction. "
                    "Make it async def to run it in the sender's task. "
                    "Silence with ASYNC_BACKEND_SYNC_RECEIVER_CHECK = 'off'."
                ),
                id="async_backend.W002",
            )
        )
    return errors


ADMIN_OVERRIDE_PAIRS = (
    ("save_model", "asave_model"),
    ("delete_model", "adelete_model"),
    ("save_formset", "asave_formset"),
    ("save_related", "asave_related"),
)


def check_admin_overrides(app_configs=None, **kwargs):
    """``async_backend.W003``: an async admin overrides a sync save hook.

    ``AsyncModelAdmin`` calls ``asave_model()``, ``adelete_model()``,
    ``asave_formset()`` and ``asave_related()``; a ``save_model()`` override
    written for Django's admin is never called.
    """
    klass = _level("ASYNC_BACKEND_OVERRIDE_CHECK")
    if klass is None:
        return []
    try:
        from django.contrib.admin.sites import all_sites

        from django_async_backend.contrib.admin.options import AsyncModelAdmin
    except Exception:  # pragma: no cover - admin not importable
        return []
    errors = []
    seen = set()
    for site in all_sites:
        for model, admin in site._registry.items():
            if not isinstance(admin, AsyncModelAdmin):
                continue
            admin_cls = type(admin)
            if admin_cls in seen:
                continue
            seen.add(admin_cls)
            for sync_name, async_name in ADMIN_OVERRIDE_PAIRS:
                sync_impl = inspect.getattr_static(admin_cls, sync_name, None)
                if sync_impl is None or sync_impl is inspect.getattr_static(
                    AsyncModelAdmin, sync_name, None
                ):
                    continue
                async_impl = inspect.getattr_static(admin_cls, async_name, None)
                if async_impl is not inspect.getattr_static(AsyncModelAdmin, async_name, None):
                    continue
                errors.append(
                    klass(
                        "%s.%s overrides %s() but not %s()." % (
                            admin_cls.__module__, admin_cls.__qualname__, sync_name, async_name
                        ),
                        hint=(
                            "The async admin calls %s(); the sync override is "
                            "never reached. Move the logic to async def %s(). "
                            "Silence with ASYNC_BACKEND_OVERRIDE_CHECK = 'off'."
                            % (async_name, async_name)
                        ),
                        obj=admin_cls,
                        id="async_backend.W003",
                    )
                )
    return errors


def register():
    checks.register(check_model_overrides, checks.Tags.models)
    checks.register(check_sync_receivers, checks.Tags.signals)
    checks.register(check_admin_overrides, checks.Tags.admin)
