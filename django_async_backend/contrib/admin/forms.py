"""Admin auth forms on the async connection."""

from django.contrib.admin.forms import (
    AdminAuthenticationForm,
    AdminPasswordChangeForm,
)
from django.contrib.auth import aauthenticate
from django.core.exceptions import ValidationError


class AsyncAdminAuthenticationForm(AdminAuthenticationForm):
    """``AdminAuthenticationForm`` whose ``ais_valid()`` authenticates with
    ``aauthenticate()``. The sync ``is_valid()`` still works."""

    async def ais_valid(self):
        if not self.is_bound:
            return False
        if self._errors is None:
            await self.afull_clean()
        return not self._errors

    async def afull_clean(self):
        # The fields are plain CharFields; only clean() queries.
        self._errors = None
        self.full_clean_sync_fields_only()
        if self._errors:
            return
        try:
            await self.aclean()
        except ValidationError as e:
            self.add_error(None, e)

    def full_clean_sync_fields_only(self):
        original_clean = self.clean
        self.clean = lambda: self.cleaned_data
        try:
            self.full_clean()
        finally:
            self.clean = original_clean

    async def aclean(self):
        username = self.cleaned_data.get("username")
        password = self.cleaned_data.get("password")
        if username is not None and password:
            self.user_cache = await aauthenticate(
                self.request, username=username, password=password
            )
            if self.user_cache is None:
                raise self.get_invalid_login_error()
            self.confirm_login_allowed(self.user_cache)
        return self.cleaned_data


class AsyncAdminPasswordChangeForm(AdminPasswordChangeForm):
    """Validation is pure (hashing); only the save is a query."""

    async def asave(self, commit=True):
        password = self.cleaned_data["new_password1"]
        self.user.set_password(password)
        if commit:
            await self.user.async_save(update_fields=["password"])
        return self.user
