"""Admin helpers on the async ORM."""

from collections import defaultdict

from django.contrib.admin.utils import quote
from django.db import (
    models,
    router,
)
from django.urls import (
    NoReverseMatch,
    reverse,
)
from django.utils.html import format_html
from django.utils.text import capfirst

from django_async_backend.db.models.deletion import Collector


class AsyncNestedObjects(Collector):
    """Async mirror of ``django.contrib.admin.utils.NestedObjects``."""

    def __init__(self, *args, force_collection=True, **kwargs):
        super().__init__(*args, force_collection=force_collection, **kwargs)
        self.edges = {}
        self.protected = set()
        self.model_objs = defaultdict(set)

    def add_edge(self, source, target):
        self.edges.setdefault(source, []).append(target)

    async def collect(self, objs, source=None, source_attr=None, **kwargs):
        # Cascades hand us async querysets (the base collector evaluates
        # them itself); the edge bookkeeping below needs the rows first.
        if hasattr(objs, "__aiter__") and not isinstance(objs, (list, tuple)):
            objs = [obj async for obj in objs]
        for obj in objs:
            if source_attr and not source_attr.endswith("+"):
                related_name = source_attr % {
                    "class": source._meta.model_name,
                    "app_label": source._meta.app_label,
                }
                self.add_edge(getattr(obj, related_name), obj)
            else:
                self.add_edge(None, obj)
            self.model_objs[obj._meta.model].add(obj)
        try:
            return await super().collect(
                objs, source_attr=source_attr, **kwargs
            )
        except models.ProtectedError as e:
            self.protected.update(e.protected_objects)
        except models.RestrictedError as e:
            self.protected.update(e.restricted_objects)

    def related_objects(self, related_model, related_fields, objs):
        qs = super().related_objects(related_model, related_fields, objs)
        return qs.select_related(
            *[related_field.name for related_field in related_fields]
        )

    def _nested(self, obj, seen, format_callback):
        if obj in seen:
            return []
        seen.add(obj)
        children = []
        for child in self.edges.get(obj, ()):
            children.extend(self._nested(child, seen, format_callback))
        if format_callback:
            ret = [format_callback(obj)]
        else:
            ret = [obj]
        if children:
            ret.append(children)
        return ret

    def nested(self, format_callback=None):
        seen = set()
        roots = []
        for root in self.edges.get(None, ()):
            roots.extend(self._nested(root, seen, format_callback))
        return roots


async def aget_deleted_objects(objs, request, admin_site):
    """Async mirror of ``django.contrib.admin.utils.get_deleted_objects``.

    ``objs`` is a list of instances (evaluate a queryset first).
    """
    from django.contrib.admin.options import EMPTY_VALUE_STRING
    from django.contrib.admin.utils import display_for_value

    objs = list(objs)
    try:
        obj = objs[0]
    except IndexError:
        return [], {}, set(), []
    else:
        using = router.db_for_write(obj._meta.model)
    collector = AsyncNestedObjects(using=using, origin=objs)
    await collector.collect(objs)
    # The format callback asks each registered admin for a per-object delete
    # permission from sync code: compute those answers first.
    for model, model_objs in collector.model_objs.items():
        if not admin_site.is_registered(model):
            continue
        model_admin = admin_site.get_model_admin(model)
        if hasattr(model_admin, "aprepare_permissions"):
            for related in model_objs:
                await model_admin.aprepare_permissions(
                    request, related, actions=("delete",)
                )
    perms_needed = set()

    def format_callback(obj):
        model = obj.__class__
        opts = obj._meta
        no_edit_link = "%s: %s" % (capfirst(opts.verbose_name), obj)
        if admin_site.is_registered(model):
            if not admin_site.get_model_admin(model).has_delete_permission(
                request, obj
            ):
                perms_needed.add(opts.verbose_name)
            try:
                admin_url = reverse(
                    "%s:%s_%s_change"
                    % (admin_site.name, opts.app_label, opts.model_name),
                    None,
                    (quote(obj.pk),),
                )
            except NoReverseMatch:
                return no_edit_link
            obj_display = display_for_value(str(obj), EMPTY_VALUE_STRING)
            return format_html(
                '{}: <a href="{}">{}</a>',
                capfirst(opts.verbose_name),
                admin_url,
                obj_display,
            )
        return no_edit_link

    to_delete = collector.nested(format_callback)
    protected = [format_callback(obj) for obj in collector.protected]
    model_count = {
        model._meta.verbose_name_plural: len(objs)
        for model, objs in collector.model_objs.items()
    }
    return to_delete, model_count, perms_needed, protected


async def awarm_permissions(user):
    """Load the user's permissions on the async connection.

    Django's sync ``has_perm()``/``has_module_perms()`` then find the
    per-user caches and run no query, so the admin's many permission checks
    (and the ``perms`` template variable) stay on this task's connection.
    Superusers short-circuit without a query.
    """
    if user.is_anonymous or getattr(user, "is_superuser", False):
        return
    if hasattr(user, "aget_all_permissions"):
        await user.aget_all_permissions()


class PreparedPaginator:
    """A sync ``Paginator`` whose count is known up front.

    Templates and admin helpers read ``paginator.count``/``num_pages`` and
    call ``get_elided_page_range()`` synchronously. Those only need the
    count, which the async view has already computed.
    """

    @staticmethod
    def build(paginator_class, object_list, per_page, count, **kwargs):
        paginator = paginator_class(object_list, per_page, **kwargs)
        paginator.__dict__["count"] = count
        return paginator
