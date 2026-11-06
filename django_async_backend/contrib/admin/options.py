"""``ModelAdmin`` and inlines with async views on the async ORM.

What changes versus Django's ``ModelAdmin``:

* ``get_queryset()`` returns ``model.async_objects``; the changelist, change
  form, delete and history views are ``async def`` and await every query,
  form validation, save, log entry and cascade collection.
* Forms are ``AsyncModelForm``s; relation fields get async choice fields and
  their rows are loaded before rendering.
* Permission checks stay Django's sync methods: the site loads the user's
  permissions on the async connection once per request, so they are cache
  reads.
* Template responses are rendered in the view's task.

``date_hierarchy`` and facet counts are precomputed by ``AsyncChangeList``;
list filters that need queries must be the async variants (plain field names
are mapped automatically; custom ones subclass ``AsyncSimpleListFilter``).
"""

import copy
import itertools
import sys
from functools import partial

from django import forms
from django.contrib import messages
from django.contrib.admin import helpers
from django.contrib.admin.exceptions import DisallowedModelAdminToField
from django.contrib.admin.options import EMPTY_VALUE_STRING as _EMPTY
from django.contrib.admin.options import (
    IS_POPUP_VAR,
    TO_FIELD_VAR,
    ActionLocation,
    InlineModelAdmin,
    ModelAdmin,
    ShowFacets,
    get_ul_class,
)
from django.contrib.admin.utils import (
    display_for_value,
    flatten_fieldsets,
    model_ngettext,
    quote,
    unquote,
)
from django.core.exceptions import (
    BadRequest,
    FieldError,
    PermissionDenied,
    ValidationError,
)
from django.core.paginator import (
    InvalidPage,
    Paginator,
)
from django.db import (
    models,
    router,
)
from django.db.models.constants import LOOKUP_SEP  # noqa: F401
from django.forms.formsets import DELETION_FIELD_NAME
from django.forms.models import modelform_defines_fields
from django.http import (
    HttpResponseBase,
    HttpResponseRedirect,
)
from django.template.response import (
    SimpleTemplateResponse,
    TemplateResponse,
)
from django.utils.text import (
    capfirst,
    get_text_list,
)
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from django_async_backend.contrib.admin import widgets as async_widgets
from django_async_backend.contrib.admin.filters import async_list_filter
from django_async_backend.contrib.admin.log import alog_actions
from django_async_backend.contrib.admin.permissions import (
    AsyncPermissionsMixin,
)
from django_async_backend.contrib.admin.utils import (
    AsyncNestedObjects,
    PreparedPaginator,
    aget_deleted_objects,
)
from django_async_backend.contrib.admin.views import AsyncChangeList
from django_async_backend.db.transaction import async_atomic
from django_async_backend.forms import (
    AsyncBaseInlineFormSet,
    AsyncModelChoiceField,
    AsyncModelForm,
    AsyncModelMultipleChoiceField,
    aprepare_instance,
    async_inlineformset_factory,
    async_modelform_factory,
    async_modelformset_factory,
)
from django_async_backend.utils.contenttypes import aget_for_model

EMPTY_VALUE_STRING = _EMPTY


class AsyncBaseModelAdminMixin(AsyncPermissionsMixin):
    """Shared by ``AsyncModelAdmin`` and ``AsyncInlineModelAdmin``."""

    form = AsyncModelForm

    def get_queryset(self, request):
        qs = self.model.async_objects.get_queryset()
        ordering = self.get_ordering(request)
        if ordering:
            qs = qs.order_by(*ordering)
        return qs

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        db = kwargs.get("using")
        if "widget" not in kwargs:
            if db_field.name in self.get_autocomplete_fields(request):
                kwargs["widget"] = async_widgets.AsyncAutocompleteSelect(
                    db_field, self.admin_site, using=db
                )
            elif db_field.name in self.raw_id_fields:
                kwargs["widget"] = async_widgets.AsyncForeignKeyRawIdWidget(
                    db_field.remote_field, self.admin_site, using=db
                )
            elif db_field.name in self.radio_fields:
                from django.contrib.admin import widgets

                kwargs["widget"] = widgets.AdminRadioSelect(
                    attrs={
                        "class": get_ul_class(
                            self.radio_fields[db_field.name]
                        ),
                    }
                )
                kwargs["empty_label"] = (
                    kwargs.get("empty_label", _("None"))
                    if db_field.blank
                    else None
                )
        if "queryset" not in kwargs:
            queryset = self.get_field_queryset(db, db_field, request)
            if queryset is None:
                queryset = db_field.remote_field.model.async_objects.using(db)
            kwargs["queryset"] = queryset
        kwargs.setdefault("form_class", AsyncModelChoiceField)
        return db_field.formfield(**kwargs)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if not db_field.remote_field.through._meta.auto_created:
            return None
        db = kwargs.get("using")
        if "widget" not in kwargs:
            from django.contrib.admin import widgets

            autocomplete_fields = self.get_autocomplete_fields(request)
            if db_field.name in autocomplete_fields:
                kwargs["widget"] = (
                    async_widgets.AsyncAutocompleteSelectMultiple(
                        db_field, self.admin_site, using=db
                    )
                )
            elif db_field.name in self.raw_id_fields:
                kwargs["widget"] = async_widgets.AsyncManyToManyRawIdWidget(
                    db_field.remote_field, self.admin_site, using=db
                )
            elif db_field.name in [
                *self.filter_vertical,
                *self.filter_horizontal,
            ]:
                kwargs["widget"] = widgets.FilteredSelectMultiple(
                    db_field.verbose_name,
                    db_field.name in self.filter_vertical,
                )
        if "queryset" not in kwargs:
            queryset = self.get_field_queryset(db, db_field, request)
            if queryset is None:
                queryset = db_field.remote_field.model.async_objects.using(db)
            kwargs["queryset"] = queryset
        kwargs.setdefault("form_class", AsyncModelMultipleChoiceField)
        form_field = db_field.formfield(**kwargs)
        from django.forms.widgets import (
            CheckboxSelectMultiple,
            SelectMultiple,
        )
        from django.utils.text import format_lazy

        if (
            isinstance(form_field.widget, SelectMultiple)
            and form_field.widget.allow_multiple_selected
            and not isinstance(
                form_field.widget,
                (
                    CheckboxSelectMultiple,
                    async_widgets.AsyncAutocompleteSelectMultiple,
                ),
            )
        ):
            msg = _(
                "Hold down “Control”, or “Command” on a Mac, to select more "
                "than one."
            )
            help_text = form_field.help_text
            form_field.help_text = (
                format_lazy("{} {}", help_text, msg) if help_text else msg
            )
        return form_field

    async def aprepare_for_render(self, request, obj, readonly_fields=None):
        """Load what the templates would otherwise fetch lazily: relations
        among the readonly fields and many-to-many initial data."""
        if obj is None or obj._state.adding:
            return
        if readonly_fields is None:
            readonly_fields = self.get_readonly_fields(request, obj)
        names = []
        for name in readonly_fields:
            try:
                field = obj._meta.get_field(name)
            except Exception:
                continue
            if field.is_relation and not field.is_cached(obj):
                names.append(name)
        for f in obj._meta.many_to_many:
            if (
                f.name not in names
                and not hasattr(obj, "_prefetched_objects_cache")
                or f.name not in getattr(obj, "_prefetched_objects_cache", {})
            ):
                names.append(f.name)
        if names:
            await obj.afetch(*names)


class AsyncModelAdmin(AsyncBaseModelAdminMixin, ModelAdmin):
    def get_changelist(self, request, **kwargs):
        return AsyncChangeList

    def get_list_filter(self, request):
        return [
            async_list_filter(entry, self.model)
            for entry in super().get_list_filter(request)
        ]

    def get_paginator(
        self,
        request,
        queryset,
        per_page,
        orphans=0,
        allow_empty_first_page=True,
    ):
        return Paginator(queryset, per_page, orphans, allow_empty_first_page)

    # ---- urls -----------------------------------------------------------

    def get_urls(self):
        from functools import update_wrapper

        from django.urls import path
        from django.views.generic import RedirectView

        def wrap(view):
            async def wrapper(*args, **kwargs):
                return await self.admin_site.admin_view(view)(*args, **kwargs)

            wrapper.model_admin = self
            return update_wrapper(wrapper, view)

        info = self.opts.app_label, self.opts.model_name
        return [
            path(
                "", wrap(self.changelist_view), name="%s_%s_changelist" % info
            ),
            path("add/", wrap(self.add_view), name="%s_%s_add" % info),
            path(
                "<path:object_id>/history/",
                wrap(self.history_view),
                name="%s_%s_history" % info,
            ),
            path(
                "<path:object_id>/delete/",
                wrap(self.delete_view),
                name="%s_%s_delete" % info,
            ),
            path(
                "<path:object_id>/change/",
                wrap(self.change_view),
                name="%s_%s_change" % info,
            ),
            path(
                "<path:object_id>/",
                wrap(
                    RedirectView.as_view(
                        pattern_name="%s:%s_%s_change"
                        % (self.admin_site.name, *info)
                    )
                ),
            ),
        ]

    # ---- forms ----------------------------------------------------------

    def get_form(self, request, obj=None, change=False, **kwargs):
        if "fields" in kwargs:
            fields = kwargs.pop("fields")
        else:
            fields = flatten_fieldsets(self.get_fieldsets(request, obj))
        excluded = self.get_exclude(request, obj)
        exclude = [] if excluded is None else list(excluded)
        readonly_fields = self.get_readonly_fields(request, obj)
        exclude.extend(readonly_fields)
        if (
            change
            and hasattr(request, "user")
            and not self.has_change_permission(request, obj)
        ):
            exclude.extend(fields)
        if (
            excluded is None
            and hasattr(self.form, "_meta")
            and self.form._meta.exclude
        ):
            exclude.extend(self.form._meta.exclude)
        exclude = exclude or None
        new_attrs = dict.fromkeys(
            f for f in readonly_fields if f in self.form.declared_fields
        )
        form = type(self.form.__name__, (self.form,), new_attrs)
        defaults = {
            "form": form,
            "fields": fields,
            "exclude": exclude,
            "formfield_callback": partial(
                self.formfield_for_dbfield, request=request
            ),
            **kwargs,
        }
        if defaults["fields"] is None and not modelform_defines_fields(
            defaults["form"]
        ):
            defaults["fields"] = forms.ALL_FIELDS
        try:
            return async_modelform_factory(self.model, **defaults)
        except FieldError as e:
            raise FieldError(
                "%s. Check fields/fieldsets/exclude attributes of class %s."
                % (e, self.__class__.__name__)
            )

    def get_changelist_formset(self, request, **kwargs):
        defaults = {
            "formfield_callback": partial(
                self.formfield_for_dbfield, request=request
            ),
            **kwargs,
        }
        return async_modelformset_factory(
            self.model,
            self.get_changelist_form(request),
            extra=0,
            fields=self.list_editable,
            **defaults,
        )

    # ---- data access ----------------------------------------------------

    async def aget_object(self, request, object_id, from_field=None):
        queryset = self.get_queryset(request)
        model = queryset.model
        field = (
            model._meta.pk
            if from_field is None
            else model._meta.get_field(from_field)
        )
        try:
            object_id = field.to_python(object_id)
            return await queryset.aget(**{field.name: object_id})
        except (model.DoesNotExist, ValidationError, ValueError):
            return None

    async def asave_form(self, request, form, change):
        return await form.asave(commit=False)

    async def asave_model(self, request, obj, form, change):
        await obj.async_save()

    async def adelete_model(self, request, obj):
        await obj.async_delete()

    async def adelete_queryset(self, request, queryset):
        await queryset.adelete()

    async def asave_formset(self, request, form, formset, change):
        await formset.asave()

    async def asave_related(self, request, form, formsets, change):
        await form.asave_m2m()
        for formset in formsets:
            await self.asave_formset(request, form, formset, change=change)

    async def alog_addition(self, request, obj, message):
        from django.contrib.admin.models import ADDITION

        return await alog_actions(
            user_id=request.user.pk,
            objs=[obj],
            action_flag=ADDITION,
            change_message=message,
            single_object=True,
        )

    async def alog_change(self, request, obj, message):
        from django.contrib.admin.models import CHANGE

        return await alog_actions(
            user_id=request.user.pk,
            objs=[obj],
            action_flag=CHANGE,
            change_message=message,
            single_object=True,
        )

    async def alog_deletions(self, request, objs):
        from django.contrib.admin.models import DELETION

        return await alog_actions(
            user_id=request.user.pk, objs=list(objs), action_flag=DELETION
        )

    async def aget_deleted_objects(self, objs, request):
        return await aget_deleted_objects(objs, request, self.admin_site)

    async def _acreate_formsets(self, request, obj, change):
        # Generic inline formsets look the parent's ContentType up through
        # Django's sync cache: fill it on the async connection first.
        if obj is not None:
            await aget_for_model(obj.__class__)
            await aget_for_model(obj.__class__, for_concrete_model=False)
        # Inline admins check their permissions against the parent object
        # from sync code paths: compute them now.
        for inline_class in self.get_inlines(request, obj if change else None):
            inline = inline_class(self.model, self.admin_site)
            if hasattr(inline, "aprepare_permissions"):
                await inline.aprepare_permissions(
                    request, obj if change else None
                )
        formsets = []
        inline_instances = []
        prefixes = {}
        get_formsets_args = [request]
        if change:
            get_formsets_args.append(obj)
        for FormSet, inline in self.get_formsets_with_inlines(
            *get_formsets_args
        ):
            prefix = FormSet.get_default_prefix()
            prefixes[prefix] = prefixes.get(prefix, 0) + 1
            if prefixes[prefix] != 1 or not prefix:
                prefix = "%s-%s" % (prefix, prefixes[prefix])
            formset_params = self.get_formset_kwargs(
                request, obj, inline, prefix
            )
            formset = FormSet(**formset_params)
            await formset.aload()

            def user_deleted_form(request, obj, formset, index, inline):
                return (
                    inline.has_delete_permission(request, obj)
                    and "{}-{}-DELETE".format(formset.prefix, index)
                    in request.POST
                )

            if not inline.has_change_permission(
                request, obj if change else None
            ):
                for index, form in enumerate(formset.initial_forms):
                    if user_deleted_form(request, obj, formset, index, inline):
                        continue
                    form._errors = {}
                    form.cleaned_data = form.initial
            formsets.append(formset)
            inline_instances.append(inline)
        return formsets, inline_instances

    async def aresponse_action(
        self, request, queryset, action_location=ActionLocation.CHANGE_LIST
    ):
        try:
            action_index = int(request.POST.get("index", 0))
        except ValueError:
            action_index = 0
        data = request.POST.copy()
        data.pop(helpers.ACTION_CHECKBOX_NAME, None)
        data.pop("index", None)
        try:
            data.update({"action": data.getlist("action")[action_index]})
        except IndexError:
            pass
        prefix = (
            action_location.value
            if action_location != ActionLocation.CHANGE_LIST
            else ""
        )
        action_form = self.action_form(data, auto_id=None, prefix=prefix)
        action_form.fields["action"].choices = (
            self._get_action_choices_with_action_location(
                request, action_location=action_location
            )
        )
        if action_form.is_valid():
            action = action_form.cleaned_data["action"]
            select_across = action_form.cleaned_data["select_across"]
            if action_location == ActionLocation.CHANGE_FORM:
                select_across = False
            actions = self._get_actions_with_action_location(
                request, action_location=action_location
            )
            if isinstance(actions[action], tuple):
                func = actions[action][0]
            else:
                func = actions[action].func
            selected = request.POST.getlist(helpers.ACTION_CHECKBOX_NAME)
            if not selected and not select_across:
                msg = _(
                    "Items must be selected in order to perform "
                    "actions on them. No items have been changed."
                )
                self.message_user(request, msg, messages.WARNING)
                return None
            if not select_across:
                queryset = queryset.filter(pk__in=selected)
            response = func(self, request, queryset)
            if hasattr(response, "__await__"):
                response = await response
            if isinstance(response, HttpResponseBase):
                return response
            return HttpResponseRedirect(request.get_full_path())
        msg = _("No action selected.")
        self.message_user(request, msg, messages.WARNING)
        return None

    # ---- views ----------------------------------------------------------

    async def changelist_view(self, request, extra_context=None):
        from django.contrib.admin.views.main import (
            ERROR_FLAG,
            IncorrectLookupParameters,
        )

        app_label = self.opts.app_label
        if not self.has_view_or_change_permission(request):
            raise PermissionDenied

        try:
            cl = self.get_changelist_instance(request)
            await cl.aget_results(request)
        except IncorrectLookupParameters:
            if ERROR_FLAG in request.GET:
                return SimpleTemplateResponse(
                    "admin/invalid_setup.html", {"title": _("Database error")}
                )
            return HttpResponseRedirect(request.path + "?" + ERROR_FLAG + "=1")

        action_failed = False
        selected = request.POST.getlist(helpers.ACTION_CHECKBOX_NAME)
        actions = self._get_actions_with_action_location(
            request, action_location=ActionLocation.CHANGE_LIST
        )
        if (
            actions
            and request.method == "POST"
            and "index" in request.POST
            and "_save" not in request.POST
        ):
            if selected:
                response = await self.aresponse_action(
                    request,
                    queryset=cl.get_queryset(request),
                    action_location=ActionLocation.CHANGE_LIST,
                )
                if response:
                    return response
                action_failed = True
            else:
                msg = _(
                    "Items must be selected in order to perform "
                    "actions on them. No items have been changed."
                )
                self.message_user(request, msg, messages.WARNING)
                action_failed = True

        if (
            actions
            and request.method == "POST"
            and helpers.ACTION_CHECKBOX_NAME in request.POST
            and "index" not in request.POST
            and "_save" not in request.POST
        ):
            if selected:
                response = await self.aresponse_action(
                    request,
                    queryset=cl.get_queryset(request),
                    action_location=ActionLocation.CHANGE_LIST,
                )
                if response:
                    return response
                action_failed = True

        if action_failed:
            return HttpResponseRedirect(request.get_full_path())

        if (
            request.method == "POST"
            and cl.list_editable
            and "_save" in request.POST
        ):
            if not self.has_change_permission(request):
                raise PermissionDenied
            FormSet = self.get_changelist_formset(request)
            modified_objects = self._get_list_editable_queryset(
                request, FormSet.get_default_prefix()
            )
            cl.formset = await self._aget_formset_with_permissions(
                request, queryset=modified_objects, for_save=True
            )
            if await cl.formset.ais_valid():
                await self._asave_formset(request, cl.formset)
                return HttpResponseRedirect(request.get_full_path())
        elif cl.list_editable and self.has_change_permission(request):
            cl.formset = await self._aget_formset_with_permissions(
                request, cl.result_list
            )

        if cl.formset:
            media = self.media + cl.formset.media
        else:
            media = self.media

        if actions:
            action_form = self.action_form(auto_id=None)
            action_form.fields["action"].choices = (
                self._get_action_choices_with_action_location(
                    request, action_location=ActionLocation.CHANGE_LIST
                )
            )
            media += action_form.media
        else:
            action_form = None

        selection_note_all = ngettext(
            "%(total_count)s selected",
            "All %(total_count)s selected",
            cl.result_count,
        )
        context = {
            **self.admin_site.each_context(request),
            "module_name": str(self.opts.verbose_name_plural),
            "selection_note": _("0 of %(cnt)s selected")
            % {"cnt": len(cl.result_list)},
            "selection_note_all": selection_note_all
            % {"total_count": cl.result_count},
            "title": cl.title,
            "subtitle": None,
            "is_popup": cl.is_popup,
            "to_field": cl.to_field,
            "cl": cl,
            "media": media,
            "has_add_permission": self.has_add_permission(request),
            "opts": cl.opts,
            "action_form": action_form,
            "actions_on_top": self.actions_on_top,
            "actions_on_bottom": self.actions_on_bottom,
            "actions_selection_counter": self.actions_selection_counter,
            "preserved_filters": self.get_preserved_filters(request),
            **(extra_context or {}),
        }
        request.current_app = self.admin_site.name
        return TemplateResponse(
            request,
            self.change_list_template
            or [
                "admin/%s/%s/change_list.html"
                % (app_label, self.opts.model_name),
                "admin/%s/change_list.html" % app_label,
                "admin/change_list.html",
            ],
            context,
        )

    async def _aget_formset_with_permissions(
        self, request, queryset, for_save=False
    ):
        FormSet = self.get_changelist_formset(request)
        if for_save:
            formset = FormSet(
                data=request.POST, files=request.FILES, queryset=queryset
            )
        else:
            formset = FormSet(queryset=queryset)
        await formset.aload()
        for form in formset.forms:
            await self.aprepare_permissions(
                request, form.instance, actions=("change",)
            )
        for form in formset.forms:
            if not self.has_change_permission(request, form.instance):
                for field_name in self.list_editable:
                    form.fields.pop(field_name, None)
        return formset

    async def _asave_formset(self, request, formset):
        changecount = 0
        async with async_atomic(using=router.db_for_write(self.model)):
            for form in formset.forms:
                if not self.has_change_permission(request, form.instance):
                    continue
                if form.has_changed():
                    obj = await self.asave_form(request, form, change=True)
                    if obj._state.adding:
                        raise BadRequest(
                            "list_editable does not allow adding."
                        )
                    await self.asave_model(request, obj, form, change=True)
                    await self.asave_related(
                        request, form, formsets=[], change=True
                    )
                    change_msg = self.construct_change_message(
                        request, form, None
                    )
                    await self.alog_change(request, obj, change_msg)
                    changecount += 1
        if changecount:
            msg = ngettext(
                "%(count)s %(name)s was changed successfully.",
                "%(count)s %(name)s were changed successfully.",
                changecount,
            ) % {
                "count": changecount,
                "name": model_ngettext(self.opts, changecount),
            }
            self.message_user(request, msg, messages.SUCCESS)

    async def add_view(self, request, form_url="", extra_context=None):
        return await self.changeform_view(
            request, None, form_url, extra_context
        )

    async def change_view(
        self, request, object_id, form_url="", extra_context=None
    ):
        return await self.changeform_view(
            request, object_id, form_url, extra_context
        )

    async def changeform_view(
        self, request, object_id=None, form_url="", extra_context=None
    ):
        if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
            return await self._achangeform_view(
                request, object_id, form_url, extra_context
            )
        async with async_atomic(using=router.db_for_write(self.model)):
            return await self._achangeform_view(
                request, object_id, form_url, extra_context
            )

    async def _achangeform_view(
        self, request, object_id, form_url, extra_context
    ):
        to_field = request.POST.get(
            TO_FIELD_VAR, request.GET.get(TO_FIELD_VAR)
        )
        if to_field and not self.to_field_allowed(request, to_field):
            raise DisallowedModelAdminToField(
                "The field %s cannot be referenced." % to_field
            )

        if request.method == "POST" and "_saveasnew" in request.POST:
            object_id = None

        add = object_id is None

        if add:
            if not self.has_add_permission(request):
                raise PermissionDenied
            obj = None
        else:
            obj = await self.aget_object(request, unquote(object_id), to_field)
            await self.aprepare_permissions(request, obj)
            if not self.has_view_or_change_permission(request, obj):
                raise PermissionDenied
            if obj is None:
                return self._get_obj_does_not_exist_redirect(
                    request, self.opts, object_id
                )

        action_form = None
        actions = self._get_actions_with_action_location(
            request, action_location=ActionLocation.CHANGE_FORM
        )
        if actions and not add:
            action_location = ActionLocation.CHANGE_FORM
            action_form = self.action_form(
                auto_id=None, prefix=action_location.value
            )
            action_form.fields["action"].choices = (
                self._get_action_choices_with_action_location(
                    request, action_location=action_location
                )
            )

        fieldsets = self.get_fieldsets(request, obj)
        ModelForm = self.get_form(
            request, obj, change=not add, fields=flatten_fieldsets(fieldsets)
        )
        if obj is not None:
            await aprepare_instance(obj, ModelForm)

        if request.method == "POST":
            if (
                action_form
                and action_form["action"].html_name in request.POST
                and "_save" not in request.POST
                and "_continue" not in request.POST
                and "_addanother" not in request.POST
            ):
                selected = request.POST.getlist(helpers.ACTION_CHECKBOX_NAME)
                if len(selected) != 1 or selected[0] != str(obj.pk):
                    raise BadRequest
                queryset = self.get_queryset(request)
                if response := await self.aresponse_action(
                    request,
                    queryset,
                    action_location=ActionLocation.CHANGE_FORM,
                ):
                    return response
                return HttpResponseRedirect(request.get_full_path())
            if not add and not self.has_change_permission(request, obj):
                raise PermissionDenied
            form = ModelForm(request.POST, request.FILES, instance=obj)
            formsets, inline_instances = await self._acreate_formsets(
                request, form.instance, change=not add
            )
            form_validated = await form.ais_valid()
            if form_validated:
                new_object = await self.asave_form(
                    request, form, change=not add
                )
            else:
                new_object = form.instance
            formsets_valid = True
            for formset in formsets:
                if not await formset.ais_valid():
                    formsets_valid = False
            if formsets_valid and form_validated:
                await self.asave_model(request, new_object, form, not add)
                await self.asave_related(request, form, formsets, not add)
                change_message = self.construct_change_message(
                    request, form, formsets, add
                )
                if add:
                    await self.alog_addition(
                        request, new_object, change_message
                    )
                    return self.response_add(request, new_object)
                await self.alog_change(request, new_object, change_message)
                return self.response_change(request, new_object)
            form_validated = False
        else:
            if add:
                initial = self.get_changeform_initial_data(request)
                form = ModelForm(initial=initial)
                formsets, inline_instances = await self._acreate_formsets(
                    request, form.instance, change=False
                )
            else:
                form = ModelForm(instance=obj)
                formsets, inline_instances = await self._acreate_formsets(
                    request, obj, change=True
                )

        await form.aload_choices()
        for formset in formsets:
            await formset.aload()

        if not add and not self.has_change_permission(request, obj):
            readonly_fields = flatten_fieldsets(fieldsets)
        else:
            readonly_fields = self.get_readonly_fields(request, obj)
        await self.aprepare_for_render(request, obj, readonly_fields)
        admin_form = helpers.AdminForm(
            form,
            list(fieldsets),
            (
                self.get_prepopulated_fields(request, obj)
                if add or self.has_change_permission(request, obj)
                else {}
            ),
            readonly_fields,
            model_admin=self,
        )
        media = self.media + admin_form.media

        inline_formsets = self.get_inline_formsets(
            request, formsets, inline_instances, obj
        )
        for inline_formset in inline_formsets:
            media += inline_formset.media
        if action_form:
            media += action_form.media

        if add:
            title = _("Add %s")
        elif self.has_change_permission(request, obj):
            title = _("Change %s")
        else:
            title = _("View %s")
        context = {
            **self.admin_site.each_context(request),
            "title": title % self.opts.verbose_name,
            "subtitle": (
                display_for_value(str(obj), EMPTY_VALUE_STRING)
                if obj
                else None
            ),
            "adminform": admin_form,
            "object_id": object_id,
            "original": obj,
            "is_popup": IS_POPUP_VAR in request.POST
            or IS_POPUP_VAR in request.GET,
            "source_model": request.GET.get("_source_model"),
            "to_field": to_field,
            "media": media,
            "action_form": action_form,
            "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
            "inline_admin_formsets": inline_formsets,
            "errors": helpers.AdminErrorList(form, formsets),
            "preserved_filters": self.get_preserved_filters(request),
        }
        if (
            request.method == "POST"
            and not form_validated
            and "_saveasnew" in request.POST
        ):
            context["show_save"] = False
            context["show_save_and_continue"] = False
            add = False
        context.update(extra_context or {})

        # render_change_form() reads the ContentType cache: fill it here.
        await aget_for_model(self.model, for_concrete_model=False)
        return self.render_change_form(
            request,
            context,
            add=add,
            change=not add,
            obj=obj,
            form_url=form_url,
        )

    async def delete_view(self, request, object_id, extra_context=None):
        if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
            return await self._adelete_view(request, object_id, extra_context)
        async with async_atomic(using=router.db_for_write(self.model)):
            return await self._adelete_view(request, object_id, extra_context)

    async def _adelete_view(self, request, object_id, extra_context):
        app_label = self.opts.app_label
        to_field = request.POST.get(
            TO_FIELD_VAR, request.GET.get(TO_FIELD_VAR)
        )
        if to_field and not self.to_field_allowed(request, to_field):
            raise DisallowedModelAdminToField(
                "The field %s cannot be referenced." % to_field
            )
        obj = await self.aget_object(request, unquote(object_id), to_field)
        await self.aprepare_permissions(request, obj)
        if not self.has_delete_permission(request, obj):
            raise PermissionDenied
        if obj is None:
            return self._get_obj_does_not_exist_redirect(
                request, self.opts, object_id
            )

        (
            deleted_objects,
            model_count,
            perms_needed,
            protected,
        ) = await self.aget_deleted_objects([obj], request)

        if request.POST and not protected:
            if perms_needed:
                raise PermissionDenied
            obj_display = str(obj)
            attr = str(to_field) if to_field else self.opts.pk.attname
            obj_id = obj.serializable_value(attr)
            await self.alog_deletions(request, [obj])
            await self.adelete_model(request, obj)
            return self.response_delete(request, obj_display, obj_id)

        object_name = str(self.opts.verbose_name)
        if perms_needed or protected:
            title = _("Cannot delete %(name)s") % {"name": object_name}
        else:
            title = _("Delete")
        context = {
            **self.admin_site.each_context(request),
            "title": title,
            "subtitle": None,
            "object_name": object_name,
            "object": obj,
            "escaped_object": display_for_value(str(obj), EMPTY_VALUE_STRING),
            "deleted_objects": deleted_objects,
            "delete_confirmation_max_display": self.delete_confirmation_max_display,
            "model_count": dict(model_count).items(),
            "perms_lacking": perms_needed,
            "protected": protected,
            "opts": self.opts,
            "app_label": app_label,
            "preserved_filters": self.get_preserved_filters(request),
            "is_popup": IS_POPUP_VAR in request.POST
            or IS_POPUP_VAR in request.GET,
            "to_field": to_field,
            **(extra_context or {}),
        }
        return self.render_delete_form(request, context)

    async def history_view(self, request, object_id, extra_context=None):
        from django.contrib.admin.models import LogEntry
        from django.contrib.admin.views.main import PAGE_VAR

        model = self.model
        obj = await self.aget_object(request, unquote(object_id))
        if obj is None:
            return self._get_obj_does_not_exist_redirect(
                request, model._meta, object_id
            )
        await self.aprepare_permissions(request, obj)
        if not self.has_view_or_change_permission(request, obj):
            raise PermissionDenied

        app_label = self.opts.app_label
        content_type = await aget_for_model(model, for_concrete_model=False)
        action_list = (
            LogEntry.async_objects.filter(
                object_id=unquote(object_id), content_type=content_type
            )
            .select_related("user", "content_type")
            .order_by("action_time")
        )
        count = await action_list.acount()
        paginator = PreparedPaginator.build(Paginator, action_list, 100, count)
        try:
            page_number = int(request.GET.get(PAGE_VAR, 1))
        except ValueError:
            page_number = 1
        try:
            page_obj = paginator.page(page_number)
        except InvalidPage:
            page_obj = paginator.page(1)
        page_obj.object_list = [obj async for obj in page_obj.object_list]
        page_range = paginator.get_elided_page_range(page_obj.number)

        context = {
            **self.admin_site.each_context(request),
            "title": _("Change history: %s")
            % display_for_value(str(obj), EMPTY_VALUE_STRING),
            "subtitle": None,
            "action_list": page_obj,
            "page_range": page_range,
            "page_var": PAGE_VAR,
            "pagination_required": count > 100,
            "module_name": str(capfirst(self.opts.verbose_name_plural)),
            "object": obj,
            "opts": self.opts,
            "preserved_filters": self.get_preserved_filters(request),
            **(extra_context or {}),
        }
        request.current_app = self.admin_site.name
        return TemplateResponse(
            request,
            self.object_history_template
            or [
                "admin/%s/%s/object_history.html"
                % (app_label, self.opts.model_name),
                "admin/%s/object_history.html" % app_label,
                "admin/object_history.html",
            ],
            context,
        )


# ---------------------------------------------------------------- inlines ----


class AsyncInlineModelAdmin(AsyncBaseModelAdminMixin, InlineModelAdmin):
    formset = AsyncBaseInlineFormSet

    def get_formset(self, request, obj=None, **kwargs):
        if "fields" in kwargs:
            fields = kwargs.pop("fields")
        else:
            fields = flatten_fieldsets(self.get_fieldsets(request, obj))
        excluded = self.get_exclude(request, obj)
        exclude = [] if excluded is None else list(excluded)
        exclude.extend(self.get_readonly_fields(request, obj))
        if (
            excluded is None
            and hasattr(self.form, "_meta")
            and self.form._meta.exclude
        ):
            exclude.extend(self.form._meta.exclude)
        exclude = exclude or None
        can_delete = self.can_delete and self.has_delete_permission(
            request, obj
        )
        defaults = {
            "form": self.form,
            "formset": self.formset,
            "fk_name": self.fk_name,
            "fields": fields,
            "exclude": exclude,
            "formfield_callback": partial(
                self.formfield_for_dbfield, request=request
            ),
            "extra": self.get_extra(request, obj, **kwargs),
            "min_num": self.get_min_num(request, obj, **kwargs),
            "max_num": self.get_max_num(request, obj, **kwargs),
            "can_delete": can_delete,
            **kwargs,
        }

        base_model_form = defaults["form"]
        if not issubclass(base_model_form, AsyncModelForm):
            base_model_form = type(
                base_model_form.__name__, (AsyncModelForm, base_model_form), {}
            )
        can_change = (
            self.has_change_permission(request, obj) if request else True
        )
        can_add = self.has_add_permission(request, obj) if request else True
        delete_confirmation_max_display = (
            sys.maxsize
            if self.delete_confirmation_max_display is None
            else self.delete_confirmation_max_display
        )

        class AsyncDeleteProtectedModelForm(base_model_form):
            async def ahand_clean_DELETE(self):
                if not self.cleaned_data.get(DELETION_FIELD_NAME, False):
                    return
                if self.instance._state.adding:
                    return
                using = router.db_for_write(self._meta.model)
                collector = AsyncNestedObjects(using=using)
                await collector.collect([self.instance])
                if collector.protected:
                    objs = []
                    protected = itertools.islice(
                        collector.protected, delete_confirmation_max_display
                    )
                    for p in protected:
                        objs.append(
                            _("%(class_name)s %(instance)s")
                            % {
                                "class_name": p._meta.verbose_name,
                                "instance": p,
                            }
                        )
                    params = {
                        "class_name": self._meta.model._meta.verbose_name,
                        "instance": self.instance,
                    }
                    remaining_object_count = (
                        len(collector.protected)
                        - delete_confirmation_max_display
                    )
                    if (
                        remaining_object_count > 0
                        and delete_confirmation_max_display
                    ):
                        related = (
                            _(", ").join(str(i) for i in objs)
                            + _(", ")
                            + ngettext(
                                "…and %(count)d more object.",
                                "…and %(count)d more objects.",
                                remaining_object_count,
                            )
                            % {"count": remaining_object_count}
                        )
                    else:
                        related = get_text_list(objs, _("and"))
                    params["related_objects"] = related
                    msg = _(
                        "Deleting %(class_name)s %(instance)s would require "
                        "deleting the following protected related objects: "
                        "%(related_objects)s"
                    )
                    raise ValidationError(
                        msg, code="deleting_protected", params=params
                    )

            async def afull_clean(self):
                await super().afull_clean()
                if self.is_bound and self.cleaned_data is not None:
                    try:
                        await self.ahand_clean_DELETE()
                    except ValidationError as e:
                        self.add_error(None, e)

            def has_changed(self):
                if not can_change and not self.instance._state.adding:
                    return False
                if not can_add and self.instance._state.adding:
                    return False
                return super().has_changed()

        defaults["form"] = AsyncDeleteProtectedModelForm

        if defaults["fields"] is None and not modelform_defines_fields(
            defaults["form"]
        ):
            defaults["fields"] = forms.ALL_FIELDS

        return async_inlineformset_factory(
            self.parent_model, self.model, **defaults
        )


class AsyncStackedInline(AsyncInlineModelAdmin):
    template = "admin/edit_inline/stacked.html"


class AsyncTabularInline(AsyncInlineModelAdmin):
    template = "admin/edit_inline/tabular.html"


class AsyncGenericInlineModelAdmin(AsyncBaseModelAdminMixin, InlineModelAdmin):
    """Inline over a ``GenericForeignKey`` (``GenericInlineModelAdmin``)."""

    ct_field = "content_type"
    ct_fk_field = "object_id"
    formset = None  # AsyncBaseGenericInlineFormSet, resolved lazily

    @property
    def checks_class(self):
        from django.contrib.contenttypes.admin import (
            GenericInlineModelAdminChecks,
        )

        return GenericInlineModelAdminChecks

    def get_formset(self, request, obj=None, **kwargs):
        from django_async_backend.forms.models import (
            AsyncBaseGenericInlineFormSet,
            async_generic_inlineformset_factory,
        )

        if "fields" in kwargs:
            fields = kwargs.pop("fields")
        else:
            fields = flatten_fieldsets(self.get_fieldsets(request, obj))
        excluded = self.get_exclude(request, obj)
        exclude = [
            *(excluded or []),
            *self.get_readonly_fields(request, obj),
        ]
        if (
            excluded is None
            and hasattr(self.form, "_meta")
            and self.form._meta.exclude
        ):
            exclude.extend(self.form._meta.exclude)
        exclude = exclude or None
        can_delete = self.can_delete and self.has_delete_permission(
            request, obj
        )
        defaults = {
            "ct_field": self.ct_field,
            "fk_field": self.ct_fk_field,
            "form": self.form,
            "formfield_callback": partial(
                self.formfield_for_dbfield, request=request
            ),
            "formset": self.formset or AsyncBaseGenericInlineFormSet,
            "extra": self.get_extra(request, obj),
            "can_delete": can_delete,
            "can_order": False,
            "fields": fields,
            "min_num": self.get_min_num(request, obj),
            "max_num": self.get_max_num(request, obj),
            "exclude": exclude,
            **kwargs,
        }
        base_model_form = defaults["form"]
        if not issubclass(base_model_form, AsyncModelForm):
            base_model_form = type(
                base_model_form.__name__, (AsyncModelForm, base_model_form), {}
            )
        can_change = (
            self.has_change_permission(request, obj) if request else True
        )
        can_add = self.has_add_permission(request, obj) if request else True

        class PermissionProtectedModelForm(base_model_form):
            def has_changed(self):
                if not can_change and not self.instance._state.adding:
                    return False
                if not can_add and self.instance._state.adding:
                    return False
                return super().has_changed()

        defaults["form"] = PermissionProtectedModelForm
        if defaults["fields"] is None and not modelform_defines_fields(
            defaults["form"]
        ):
            defaults["fields"] = forms.ALL_FIELDS
        return async_generic_inlineformset_factory(self.model, **defaults)


class AsyncGenericStackedInline(AsyncGenericInlineModelAdmin):
    template = "admin/edit_inline/stacked.html"


class AsyncGenericTabularInline(AsyncGenericInlineModelAdmin):
    template = "admin/edit_inline/tabular.html"


# Unused-import guards for names kept for parity with Django's module.
_ = _
copy = copy
quote = quote
models = models
