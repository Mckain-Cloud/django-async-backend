"""Load Django fixtures on the async connection.

``loaddata`` is a synchronous management command: it deserializes with
Django's serializers (pure Python) and then saves every object on the sync
connection. Inside an async test transaction those rows would be invisible.
:func:`aload_fixtures` keeps the discovery and deserialization and saves with
``async_save_base()`` instead, so the data lands in the caller's transaction.
"""

import os
import warnings

from django.core import serializers
from django.core.management.color import no_style
from django.core.serializers import base as serializers_base
from django.db import (
    DEFAULT_DB_ALIAS,
    models,
    router,
)

from django_async_backend.db import async_connections


async def _aexecute(connection, sql):
    async with await connection.cursor() as cursor:
        await cursor.execute(sql)


async def _asave_deserialized(obj, using):
    """Async twin of ``DeserializedObject.save()``."""
    await obj.object._async_save_base(using=using, raw=True)
    if obj.m2m_data:
        for accessor_name, object_list in obj.m2m_data.items():
            manager = getattr(obj.object, accessor_name).async_objects
            await manager.aset(object_list)
    obj.m2m_data = None


async def _asave_deferred_fields(obj, using):
    """Async twin of ``DeserializedObject.save_deferred_fields()``."""
    obj.m2m_data = {}
    for field, field_value in obj.deferred_fields.items():
        opts = obj.object._meta
        label = opts.app_label + "." + opts.model_name
        if isinstance(field.remote_field, models.ManyToManyRel):
            try:
                values = serializers_base.deserialize_m2m_values(
                    field, field_value, using, handle_forward_references=False
                )
            except serializers_base.M2MDeserializationError as e:
                raise serializers_base.DeserializationError.WithData(
                    e.original_exc, label, obj.object.pk, e.pk
                )
            obj.m2m_data[field.name] = values
        elif isinstance(field.remote_field, models.ManyToOneRel):
            try:
                value = serializers_base.deserialize_fk_value(
                    field, field_value, using, handle_forward_references=False
                )
            except Exception as e:
                raise serializers_base.DeserializationError.WithData(
                    e, label, obj.object.pk, field_value
                )
            setattr(obj.object, field.attname, value)
    await _asave_deserialized(obj, using)


def _loaddata_command(using, ignore):
    from django.core.management.commands.loaddata import Command

    cmd = Command()
    cmd.using = using
    cmd.ignore = ignore
    cmd.app_label = None
    cmd.verbosity = 0
    cmd.format = None
    cmd.excluded_models, cmd.excluded_apps = set(), set()
    cmd.serialization_formats = serializers.get_public_serializer_formats()
    return cmd


async def aload_fixtures(
    fixture_labels, *, using=DEFAULT_DB_ALIAS, ignore=False
):
    """Load ``fixture_labels`` (the same labels ``loaddata`` accepts) on the
    async connection ``using``. Returns the number of objects saved.

    Natural keys are not supported: resolving them queries the database
    through the sync manager.
    """
    cmd = _loaddata_command(using, ignore)
    connection = async_connections[using]
    deferrable = connection.features.can_defer_constraint_checks

    loaded_models = set()
    deferred = []
    loaded = 0

    if deferrable:
        await _aexecute(connection, "SET CONSTRAINTS ALL DEFERRED")
    try:
        for label in fixture_labels:
            for fixture_file, _fixture_dir, fixture_name in cmd.find_fixtures(
                label
            ):
                _, ser_fmt, cmp_fmt = cmd.parse_name(
                    os.path.basename(fixture_file)
                )
                open_method, mode = cmd.compression_formats[cmp_fmt]
                fixture = open_method(fixture_file, mode)
                objects_in_fixture = 0
                try:
                    objects = serializers.deserialize(
                        ser_fmt,
                        fixture,
                        using=using,
                        ignorenonexistent=ignore,
                        handle_forward_references=True,
                    )
                    for obj in objects:
                        objects_in_fixture += 1
                        model = obj.object.__class__
                        if router.allow_migrate_model(using, model):
                            loaded_models.add(model)
                            await _asave_deserialized(obj, using)
                            loaded += 1
                        if obj.deferred_fields:
                            deferred.append(obj)
                except Exception as e:
                    e.args = (
                        "Problem installing fixture '%s': %s"
                        % (fixture_file, e),
                    )
                    raise
                finally:
                    fixture.close()
                if objects_in_fixture == 0:
                    warnings.warn(
                        "No fixture data found for '%s'. (File format may be "
                        "invalid.)" % fixture_name,
                        RuntimeWarning,
                    )
        for obj in deferred:
            await _asave_deferred_fields(obj, using)
    finally:
        if deferrable:
            # Like loaddata: re-enable checks (which raises on a violation)
            # and then put the transaction back into deferred mode.
            await _aexecute(connection, "SET CONSTRAINTS ALL IMMEDIATE")
            await _aexecute(connection, "SET CONSTRAINTS ALL DEFERRED")

    if loaded:
        for line in connection.ops.sequence_reset_sql(
            no_style(), loaded_models
        ):
            await _aexecute(connection, line)
    return loaded
