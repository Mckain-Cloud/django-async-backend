"""
Runtime patch that gives every Django model the async behavior defined on
``django_async_backend.db.models.base.AsyncModelMixin``.

It exists to add async support to models we can't declare the mixin on:
auto-created models such as m2m "through" models, as well as undecorated and
third-party models.
"""

from django.db.models.base import Model

from django_async_backend.db.models.base import AsyncModelMixin
from django_async_backend.db.models.fetch import afetch
from django_async_backend.db.models.raw import patch_araw
from django_async_backend.db.models.related import patch_related_managers
from django_async_backend.db.models.validation import aclean_fields

_ORIGINAL_MODEL_ATTRS = frozenset(dir(Model))
_ASYNC_MIXIN_MEMBERS = {
    name: value
    for name, value in vars(AsyncModelMixin).items()
    if not name.startswith("__")
}
# Hand-written additions that are not part of the generated mixin.
_ASYNC_MIXIN_MEMBERS["afetch"] = afetch
_ASYNC_MIXIN_MEMBERS["aclean_fields"] = aclean_fields

_patched = False


def _patch_model():
    global _patched
    if _patched:  # pragma: no cover
        return
    for name, value in _ASYNC_MIXIN_MEMBERS.items():
        setattr(Model, name, value)
        if name not in vars(AsyncModelMixin):
            setattr(AsyncModelMixin, name, value)
    patch_related_managers()
    patch_araw()
    _patched = True
