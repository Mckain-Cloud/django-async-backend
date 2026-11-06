"""``AdminSite`` with async views.

Every admin view runs in the request's task on its async connection. The
wrapper installed by ``admin_view()`` resolves ``request.user`` with
``auser()``, loads the user's permissions once (so Django's many sync
``has_perm()`` checks are cache reads), awaits the view and renders the
template response in the task, so a stray lazy query fails loudly instead of
running on a worker thread.
"""

import inspect
from functools import update_wrapper

from asgiref.sync import sync_to_async
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import (
    REDIRECT_FIELD_NAME,
    alogin,
    alogout,
    aupdate_session_auth_hash,
)
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.views import (
    LoginView,
    LogoutView,
)
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import (
    reverse,
    reverse_lazy,
)
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect

from django_async_backend.contrib.admin import actions
from django_async_backend.contrib.admin.forms import (
    AsyncAdminAuthenticationForm,
    AsyncAdminPasswordChangeForm,
)
from django_async_backend.contrib.admin.utils import awarm_permissions
from django_async_backend.contrib.admin.views import AsyncAutocompleteJsonView


class _PreparedLogEntries(list):
    """Already filtered and ordered: the template tag's chaining is a no-op."""

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def select_related(self, *args, **kwargs):
        return self


async def arender(response):
    """Render a TemplateResponse here, in the task, through the async
    template engine, rather than later in a worker thread.

    Rendering in the view means template-response middleware
    (``process_template_response``, e.g. the debug toolbar) sees an
    already-rendered response. Set ``ASYNC_BACKEND_ADMIN_RENDER_IN_VIEW =
    False`` to hand the unrendered response back instead; Django then runs
    those hooks and renders it with the sync engine on a worker thread.
    """
    from django.conf import settings

    if not getattr(settings, "ASYNC_BACKEND_ADMIN_RENDER_IN_VIEW", True):
        return response
    if hasattr(response, "render") and callable(response.render):
        if not getattr(response, "is_rendered", True):
            from django_async_backend.template import arender_response

            response = await arender_response(response)
    return response


class AsyncAdminSite(AdminSite):
    login_form = AsyncAdminAuthenticationForm
    password_change_form = AsyncAdminPasswordChangeForm

    def __init__(self, name="admin"):
        super().__init__(name)
        self._actions = {"delete_selected": actions.adelete_selected}
        self._global_actions = self._actions.copy()

    def register(self, model_or_iterable, admin_class=None, **options):
        from django_async_backend.contrib.admin.options import AsyncModelAdmin

        if admin_class is None:
            admin_class = AsyncModelAdmin
        return super().register(model_or_iterable, admin_class, **options)

    # ---- request wrapping ---------------------------------------------

    async def aprepare_request(self, request):
        request.user = await request.auser()
        await awarm_permissions(request.user)

    def admin_view(self, view, cacheable=False):
        async def inner(request, *args, **kwargs):
            await self.aprepare_request(request)
            if not self.has_permission(request):
                if request.path == reverse(
                    "admin:logout", current_app=self.name
                ):
                    index_path = reverse("admin:index", current_app=self.name)
                    return HttpResponseRedirect(index_path)
                from django.contrib.auth.views import redirect_to_login

                return redirect_to_login(
                    request.get_full_path(),
                    reverse("admin:login", current_app=self.name),
                )
            if inspect.iscoroutinefunction(view):
                response = await view(request, *args, **kwargs)
            else:
                # Sync helper views (JS catalog, view-on-site shortcut).
                response = await sync_to_async(view)(request, *args, **kwargs)
            return await arender(response)

        if not cacheable:
            inner = never_cache(inner)
        if not getattr(view, "csrf_exempt", False):
            inner = csrf_protect(inner)
        return update_wrapper(inner, view)

    def get_urls(self):
        from django.contrib.contenttypes import views as contenttype_views
        from django.urls import (
            include,
            path,
            re_path,
        )

        def wrap(view, cacheable=False):
            async def wrapper(*args, **kwargs):
                return await self.admin_view(view, cacheable)(*args, **kwargs)

            wrapper.admin_site = self
            wrapper.login_url = reverse_lazy(
                "admin:login", current_app=self.name
            )
            return update_wrapper(wrapper, view)

        urlpatterns = [
            path("", wrap(self.index), name="index"),
            path("login/", self.login, name="login"),
            path("logout/", wrap(self.logout), name="logout"),
            path(
                "password_change/",
                wrap(self.password_change, cacheable=True),
                name="password_change",
            ),
            path(
                "password_change/done/",
                wrap(self.password_change_done, cacheable=True),
                name="password_change_done",
            ),
            path(
                "autocomplete/",
                wrap(self.autocomplete_view),
                name="autocomplete",
            ),
            path(
                "jsi18n/",
                wrap(self.i18n_javascript, cacheable=True),
                name="jsi18n",
            ),
            path(
                "r/<path:content_type_id>/<path:object_id>/",
                wrap(contenttype_views.shortcut),
                name="view_on_site",
            ),
        ]
        valid_app_labels = []
        for model, model_admin in self._registry.items():
            urlpatterns += [
                path(
                    "%s/%s/" % (model._meta.app_label, model._meta.model_name),
                    include(model_admin.urls),
                ),
            ]
            if model._meta.app_label not in valid_app_labels:
                valid_app_labels.append(model._meta.app_label)
        if valid_app_labels:
            regex = r"^(?P<app_label>" + "|".join(valid_app_labels) + ")/$"
            urlpatterns += [
                re_path(regex, wrap(self.app_index), name="app_list"),
            ]
        if self.final_catch_all_view:
            urlpatterns.append(
                re_path(r"(?P<url>.*)$", wrap(self.catch_all_view))
            )
        return urlpatterns

    # ---- views ----------------------------------------------------------

    admin_log_limit = 10

    async def aprepare_admin_log(self, request):
        """Evaluate the "Recent actions" entries now.

        The ``get_admin_log`` template tag runs a queryset while rendering;
        ``get_log_entries()`` hands it this pre-evaluated list instead.
        """
        from django.contrib.admin.models import LogEntry

        entries = [
            entry
            async for entry in LogEntry.async_objects.select_related(
                "content_type", "user"
            )
            .filter(user__pk=request.user.pk)
            .order_by("-action_time")[: self.admin_log_limit]
        ]
        request._async_admin_log = _PreparedLogEntries(entries)

    def get_log_entries(self, request):
        prepared = getattr(request, "_async_admin_log", None)
        if prepared is not None:
            return prepared
        return super().get_log_entries(request)

    async def index(self, request, extra_context=None):
        await self.aprepare_admin_log(request)
        return super().index(request, extra_context)

    async def app_index(self, request, app_label, extra_context=None):
        return super().app_index(request, app_label, extra_context)

    async def password_change_done(self, request, extra_context=None):
        return super().password_change_done(request, extra_context)

    async def autocomplete_view(self, request):
        return await AsyncAutocompleteJsonView.as_view(admin_site=self)(
            request
        )

    @method_decorator(never_cache)
    @login_not_required
    async def login(self, request, extra_context=None):
        await self.aprepare_request(request)
        redirect_url = LoginView().get_redirect_url(request) or reverse(
            "admin:index", current_app=self.name
        )
        if request.method == "GET" and self.has_permission(request):
            return HttpResponseRedirect(redirect_url)

        form_class = self.login_form or AsyncAdminAuthenticationForm
        if request.method == "POST":
            form = form_class(request, data=request.POST)
            if await form.ais_valid():
                await alogin(request, form.get_user())
                return HttpResponseRedirect(redirect_url)
        else:
            form = form_class(request)

        context = {
            **self.each_context(request),
            "title": _("Log in"),
            "subtitle": None,
            "app_path": request.get_full_path(),
            "username": request.user.get_username(),
            REDIRECT_FIELD_NAME: redirect_url,
            "form": form,
            "next": redirect_url,
            **(extra_context or {}),
        }
        request.current_app = self.name
        return await arender(
            TemplateResponse(
                request, self.login_template or "admin/login.html", context
            )
        )

    async def logout(self, request, extra_context=None):
        if request.method != "POST":
            from django.http import HttpResponseNotAllowed

            return HttpResponseNotAllowed(["POST", "OPTIONS"])
        await alogout(request)
        view = LogoutView()
        view.setup(request)
        redirect_to = view.get_success_url()
        if redirect_to != request.get_full_path():
            return HttpResponseRedirect(redirect_to)
        context = {
            **self.each_context(request),
            "site": None,
            "site_name": None,
            "title": _("Logged out"),
            "subtitle": None,
            "has_permission": False,
            **(extra_context or {}),
        }
        request.current_app = self.name
        return TemplateResponse(
            request,
            self.logout_template or "registration/logged_out.html",
            context,
        )

    async def password_change(self, request, extra_context=None):
        form_class = self.password_change_form or AsyncAdminPasswordChangeForm
        url = reverse("admin:password_change_done", current_app=self.name)
        if request.method == "POST":
            form = form_class(user=request.user, data=request.POST)
            if await form.ais_valid():  # hashing in a worker thread
                await form.asave()
                await aupdate_session_auth_hash(request, form.user)
                return HttpResponseRedirect(url)
        else:
            form = form_class(user=request.user)
        context = {
            **self.each_context(request),
            "form": form,
            "title": _("Password change"),
            "subtitle": None,
            **(extra_context or {}),
        }
        request.current_app = self.name
        return TemplateResponse(
            request,
            self.password_change_template
            or "registration/password_change_form.html",
            context,
        )
