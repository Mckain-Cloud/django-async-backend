"""Async validation of model constraints.

``Model.validate_constraints()`` calls ``constraint.validate()``, and both
``UniqueConstraint`` and ``CheckConstraint`` run queries from there. The two
functions below are mirrors of those methods on the async ORM, used by the
generated ``async_validate_constraints()``.
"""

from django.core.exceptions import ValidationError
from django.db import (
    DEFAULT_DB_ALIAS,
    DatabaseError,
    NotSupportedError,
)
from django.db.models import (
    BooleanField,
    F,
    Q,
    Value,
)
from django.db.models.constraints import (
    CheckConstraint,
    UniqueConstraint,
)
from django.db.models.expressions import Exists
from django.db.models.functions import Coalesce
from django.db.models.lookups import (
    Exact,
    IsNull,
)
from django.db.models.sql.constants import SINGLE

from django_async_backend.db import async_connections
from django_async_backend.db.models import sql as async_sql
from django_async_backend.db.transaction import async_atomic


async def aq_check(q, against, using=DEFAULT_DB_ALIAS):
    """Async mirror of ``Q.check()``."""
    query = async_sql.Query(None)
    for name, value in against.items():
        if not hasattr(value, "resolve_expression"):
            value = Value(value)
        query.add_annotation(value, name, select=False)
    query.add_annotation(Value(1), "_check")
    connection = async_connections[using]
    # This will raise a FieldError if a field is missing in "against".
    query.add_q(q)
    compiler = query.get_compiler(using=using)
    try:
        if connection.in_atomic_block:
            async with async_atomic(using=using):
                return (await compiler.execute_sql(SINGLE)) is not None
        return (await compiler.execute_sql(SINGLE)) is not None
    except DatabaseError:
        return True


async def _validate_check(constraint, model, instance, exclude, using):
    against = instance._get_field_expression_map(
        meta=model._meta, exclude=exclude
    )
    if exclude and constraint._expression_refs_exclude(
        model, constraint.condition, exclude
    ):
        return
    condition = constraint.condition
    if async_connections[using].features.supports_comparing_boolean_expr:
        condition = Coalesce(condition, True, output_field=BooleanField())
    if not await aq_check(Q(condition), against, using=using):
        raise ValidationError(
            constraint.get_violation_error_message(),
            code=constraint.violation_error_code,
        )


async def _validate_unique(constraint, model, instance, exclude, using):
    queryset = model.async_objects.using(using)
    if constraint.fields:
        lookup_kwargs = {}
        generated_field_names = []
        for field_name in constraint.fields:
            if exclude and field_name in exclude:
                return
            field = model._meta.get_field(field_name)
            if field.generated:
                if exclude and constraint._expression_refs_exclude(
                    model, field.expression, exclude
                ):
                    return
                generated_field_names.append(field.name)
            else:
                lookup_value = getattr(instance, field.attname)
                if (
                    constraint.nulls_distinct is not False
                    and lookup_value is None
                    or (
                        lookup_value == ""
                        and async_connections[
                            using
                        ].features.interprets_empty_strings_as_nulls
                    )
                ):
                    return
                lookup_kwargs[field.name] = lookup_value
        lookup_args = []
        if generated_field_names:
            field_expression_map = instance._get_field_expression_map(
                meta=model._meta, exclude=exclude
            )
            for field_name in generated_field_names:
                expression = field_expression_map[field_name]
                if constraint.nulls_distinct is False:
                    lhs = F(field_name)
                    condition = Q(Exact(lhs, expression)) | Q(
                        IsNull(lhs, True), IsNull(expression, True)
                    )
                    lookup_args.append(condition)
                else:
                    lookup_kwargs[field_name] = expression
        queryset = queryset.filter(*lookup_args, **lookup_kwargs)
    else:
        if exclude and any(
            constraint._expression_refs_exclude(model, expression, exclude)
            for expression in constraint.expressions
        ):
            return
        replacements = {
            F(field): value
            for field, value in instance._get_field_expression_map(
                meta=model._meta, exclude=exclude
            ).items()
        }
        filters = []
        for expr in constraint.expressions:
            if hasattr(expr, "get_expression_for_validation"):
                expr = expr.get_expression_for_validation()
            rhs = expr.replace_expressions(replacements)
            condition = Exact(expr, rhs)
            if constraint.nulls_distinct is False:
                condition = Q(condition) | Q(
                    IsNull(expr, True), IsNull(rhs, True)
                )
            filters.append(condition)
        queryset = queryset.filter(*filters)
    model_class_pk = instance._async_get_pk_val(model._meta)
    if not instance._state.adding and instance._async_is_pk_set(model._meta):
        queryset = queryset.exclude(pk=model_class_pk)
    if not constraint.condition:
        if await queryset.aexists():
            if (
                constraint.fields
                and constraint.violation_error_message
                == constraint.default_violation_error_message
            ):
                validation_error_message = instance.unique_error_message(
                    model, constraint.fields
                )
                raise ValidationError(
                    validation_error_message,
                    code=validation_error_message.code,
                )
            raise ValidationError(
                constraint.get_violation_error_message(),
                code=constraint.violation_error_code,
            )
    else:
        if exclude and constraint._expression_refs_exclude(
            model, constraint.condition, exclude
        ):
            return
        against = instance._get_field_expression_map(
            meta=model._meta, exclude=exclude
        )
        if await aq_check(
            constraint.condition
            & Exists(queryset.filter(constraint.condition)),
            against,
            using=using,
        ):
            raise ValidationError(
                constraint.get_violation_error_message(),
                code=constraint.violation_error_code,
            )


async def avalidate_constraint(
    constraint, model, instance, exclude=None, using=DEFAULT_DB_ALIAS
):
    """Async mirror of ``constraint.validate(model, instance, ...)``."""
    if isinstance(constraint, UniqueConstraint):
        return await _validate_unique(
            constraint, model, instance, exclude, using
        )
    if isinstance(constraint, CheckConstraint):
        return await _validate_check(
            constraint, model, instance, exclude, using
        )
    raise NotSupportedError(
        "async_full_clean() cannot validate %s constraints."
        % type(constraint).__name__
    )
