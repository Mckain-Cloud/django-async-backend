"""Async ``Model.clean_fields()``.

Django's ``clean_fields()`` calls ``field.clean()`` on every field, and
``ForeignKey.validate()`` (``OneToOneField`` too) runs an ``exists()`` query
on the sync connection. ``aclean_fields()`` mirrors ``clean_fields()`` and
performs that existence check with ``aexists()`` on the async connection;
every other field is cleaned exactly as Django does.
"""

from django.core.exceptions import ValidationError
from django.db import router
from django.db.models.fields import NOT_PROVIDED
from django.db.models.fields.related import ForeignKey

try:
    from django.db.models.expressions import DatabaseDefault
except ImportError:  # pragma: no cover

    class DatabaseDefault:  # type: ignore[no-redef]
        pass


async def _avalidate_foreign_key(field, value, model_instance):
    """Async mirror of ``ForeignKey.validate()``."""
    if field.remote_field.parent_link:
        return
    super(ForeignKey, field).validate(value, model_instance)
    if value is None:
        return
    using = router.db_for_read(field.remote_field.model, instance=model_instance)
    qs = field.remote_field.model.async_objects.using(using).filter(
        **{field.remote_field.field_name: value}
    )
    qs = qs.complex_filter(field.get_limit_choices_to())
    if not await qs.aexists():
        raise ValidationError(
            field.error_messages["invalid"],
            code="invalid",
            params={
                "model": field.remote_field.model._meta.verbose_name,
                "pk": value,
                "field": field.remote_field.field_name,
                "value": value,
            },
        )


async def _aclean_field(field, value, model_instance):
    """Async mirror of ``Field.clean()``."""
    value = field.to_python(value)
    if isinstance(field, ForeignKey):
        await _avalidate_foreign_key(field, value, model_instance)
    else:
        field.validate(value, model_instance)
    field.run_validators(value)
    return value


async def aclean_fields(self, exclude=None):
    """Async mirror of ``Model.clean_fields()``."""
    if exclude is None:
        exclude = set()
    errors = {}
    for f in self._meta.fields:
        if f.name in exclude or f.generated:
            continue
        raw_value = getattr(self, f.attname)
        if f.blank and raw_value in f.empty_values:
            continue
        if isinstance(raw_value, DatabaseDefault):
            continue
        try:
            setattr(self, f.attname, await _aclean_field(f, raw_value, self))
        except ValidationError as e:
            errors[f.name] = e.error_list
    if errors:
        raise ValidationError(errors)


__all__ = ["aclean_fields"]
_ = NOT_PROVIDED
