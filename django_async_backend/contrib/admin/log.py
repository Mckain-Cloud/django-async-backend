"""Admin log entries on the async connection."""

import json

from django_async_backend.utils.contenttypes import aget_for_model


async def alog_actions(
    user_id, objs, action_flag, change_message="", *, single_object=False
):
    """Async mirror of ``LogEntryManager.log_actions()``.

    ``objs`` must be a list of instances (not a queryset).
    """
    from django.contrib.admin.models import LogEntry

    if isinstance(change_message, list):
        change_message = json.dumps(change_message)
    entries = []
    for obj in objs:
        content_type = await aget_for_model(
            obj.__class__, for_concrete_model=False
        )
        entries.append(
            LogEntry(
                user_id=user_id,
                content_type_id=content_type.id,
                object_id=obj.pk,
                object_repr=str(obj)[:200],
                action_flag=action_flag,
                change_message=change_message,
            )
        )
    if len(entries) == 1:
        instance = entries[0]
        await instance.async_save()
        return instance if single_object else [instance]
    return await LogEntry.async_objects.abulk_create(entries)
