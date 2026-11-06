"""Built-in admin actions on the async ORM."""

from django.contrib import messages
from django.contrib.admin import helpers
from django.contrib.admin.decorators import action
from django.contrib.admin.utils import model_ngettext
from django.core.exceptions import PermissionDenied
from django.template.response import TemplateResponse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy


@action(
    permissions=["delete"],
    description=gettext_lazy("Delete selected %(verbose_name_plural)s"),
)
async def adelete_selected(modeladmin, request, queryset):
    """Async mirror of ``django.contrib.admin.actions.delete_selected``."""
    opts = modeladmin.model._meta
    app_label = opts.app_label

    objs = [obj async for obj in queryset]
    (
        deletable_objects,
        model_count,
        perms_needed,
        protected,
    ) = await modeladmin.aget_deleted_objects(objs, request)

    if request.POST.get("post") and not protected:
        if perms_needed:
            raise PermissionDenied
        n = len(objs)
        if n:
            await modeladmin.alog_deletions(request, objs)
            await modeladmin.adelete_queryset(request, queryset)
            modeladmin.message_user(
                request,
                _("Successfully deleted %(count)d %(items)s.")
                % {"count": n, "items": model_ngettext(modeladmin.opts, n)},
                messages.SUCCESS,
            )
        return None

    objects_name = model_ngettext(modeladmin.opts, len(objs))
    if perms_needed or protected:
        title = _("Cannot delete %(name)s") % {"name": objects_name}
    else:
        title = _("Delete multiple objects")

    context = {
        **modeladmin.admin_site.each_context(request),
        "title": title,
        "subtitle": None,
        "objects_name": str(objects_name),
        "deletable_objects": [deletable_objects],
        "delete_confirmation_max_display": (
            modeladmin.delete_confirmation_max_display
        ),
        "model_count": dict(model_count).items(),
        "queryset": objs,
        "perms_lacking": perms_needed,
        "protected": protected,
        "opts": opts,
        "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
        "media": modeladmin.media,
    }
    request.current_app = modeladmin.admin_site.name
    return TemplateResponse(
        request,
        modeladmin.delete_selected_confirmation_template
        or [
            "admin/%s/%s/delete_selected_confirmation.html"
            % (app_label, opts.model_name),
            "admin/%s/delete_selected_confirmation.html" % app_label,
            "admin/delete_selected_confirmation.html",
        ],
        context,
    )
