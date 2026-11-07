"""Async validation for plain (non-model) forms.

``AsyncFormMixin`` gives any ``django.forms.Form`` ``ais_valid()``,
``aerrors()`` and ``afull_clean()``: fields with an async clean
(``AsyncModelChoiceField``, ``AsyncModelMultipleChoiceField``) are awaited,
``aclean_<field>()`` and ``aclean()`` hooks are honoured, and everything
else runs Django's sync code, which touches no database for plain fields.
"""

from django import forms
from django.core.exceptions import ValidationError


class AsyncFormMixin:
    async def aerrors(self):
        if self._errors is None:
            await self.afull_clean()
        return self._errors

    async def ais_valid(self):
        return self.is_bound and not await self.aerrors()

    async def afull_clean(self):
        self._errors = forms.utils.ErrorDict(renderer=self.renderer)
        if not self.is_bound:
            return
        self.cleaned_data = {}
        if self.empty_permitted and not self.has_changed():
            return
        await self._aclean_fields()
        await self._aclean_form()
        await self._apost_clean()

    async def _aclean_fields(self):
        for name, bf in self._bound_items():
            field = bf.field
            try:
                if hasattr(field, "_aclean_bound_field"):
                    value = await field._aclean_bound_field(bf)
                else:
                    value = field._clean_bound_field(bf)
                self.cleaned_data[name] = value
                cleaner = getattr(self, "aclean_%s" % name, None)
                if cleaner is not None:
                    self.cleaned_data[name] = await cleaner()
                elif hasattr(self, "clean_%s" % name):
                    self.cleaned_data[name] = getattr(self, "clean_%s" % name)()
            except ValidationError as e:
                self.add_error(name, e)

    async def _aclean_form(self):
        try:
            if hasattr(self, "aclean"):
                cleaned_data = await self.aclean()
            else:
                cleaned_data = self.clean()
        except ValidationError as e:
            self.add_error(None, e)
        else:
            if cleaned_data is not None:
                self.cleaned_data = cleaned_data

    async def _apost_clean(self):
        pass


class AsyncForm(AsyncFormMixin, forms.Form):
    pass
