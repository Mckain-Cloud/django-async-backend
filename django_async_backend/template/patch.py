"""Let Django's sync ``resolve()`` return values resolved asynchronously.

The async engine renders unknown nodes with their own ``render()`` after
resolving their expressions with ``await``. The results are kept in a
per-render dict in ``context.render_context``; the shims below make
``FilterExpression.resolve()`` and ``Variable.resolve()`` return those
values when present, and behave exactly as before otherwise.
"""

from django.template.base import (
    FilterExpression,
    Variable,
)

from django_async_backend.template.engine import PRERESOLVED_KEY

_original_filter_resolve = FilterExpression.resolve
_original_variable_resolve = Variable.resolve
_MISSING = object()


def _lookup(self, context):
    render_context = getattr(context, "render_context", None)
    if render_context is None:
        return _MISSING
    store = render_context.dicts[0].get(PRERESOLVED_KEY)
    if not store:
        return _MISSING
    return store.get(id(self), _MISSING)


def _filter_resolve(self, context, ignore_failures=False):
    value = _lookup(self, context)
    if value is _MISSING:
        return _original_filter_resolve(self, context, ignore_failures)
    return value


def _variable_resolve(self, context):
    value = _lookup(self, context)
    if value is _MISSING:
        return _original_variable_resolve(self, context)
    return value


def patch_template_resolve():
    FilterExpression.resolve = _filter_resolve
    Variable.resolve = _variable_resolve


def unpatch_template_resolve():
    FilterExpression.resolve = _original_filter_resolve
    Variable.resolve = _original_variable_resolve


def is_patched():
    return FilterExpression.resolve is _filter_resolve
