"""Admin widgets that render from rows loaded ahead of time.

``AutocompleteSelect`` and ``ForeignKeyRawIdWidget`` query the database while
rendering (to label the selected values). ``AsyncModelForm.aload_choices()``
loads those rows on the async connection and leaves them on the field /
widget; these subclasses read them instead of querying.
"""

from django.contrib.admin.widgets import (
    AutocompleteSelect,
    AutocompleteSelectMultiple,
    ForeignKeyRawIdWidget,
    ManyToManyRawIdWidget,
    RelatedFieldWidgetWrapper,
)
from django.urls import (
    NoReverseMatch,
    reverse,
)
from django.utils.text import Truncator


def unwrap_widget(widget):
    while isinstance(widget, RelatedFieldWidgetWrapper):
        widget = widget.widget
    return widget


class _PreloadedAutocompleteMixin:
    needs_selected_rows_only = True

    def optgroups(self, name, value, attr=None):
        default = (None, [], 0)
        groups = [default]
        has_selected = False
        selected_choices = {
            str(v)
            for v in value
            if str(v) not in self.choices.field.empty_values
        }
        if not self.is_required and not self.allow_multiple_selected:
            default[1].append(self.create_option(name, "", "", False, 0))
        remote_model_opts = self.field.remote_field.model._meta
        to_field_name = getattr(
            self.field.remote_field, "field_name", remote_model_opts.pk.attname
        )
        to_field_name = remote_model_opts.get_field(to_field_name).attname
        rows = getattr(self.choices.field, "_async_choices", None)
        if rows is None:
            raise TypeError(
                "Selected rows for %r have not been loaded; await "
                "form.aload_choices() before rendering." % name
            )
        choices = (
            (
                getattr(obj, to_field_name),
                self.choices.field.label_from_instance(obj),
            )
            for obj in rows
            if str(getattr(obj, to_field_name)) in selected_choices
        )
        for option_value, option_label in choices:
            selected = str(option_value) in value and (
                has_selected is False or self.allow_multiple_selected
            )
            has_selected |= selected
            index = len(default[1])
            subgroup = default[1]
            subgroup.append(
                self.create_option(
                    name, option_value, option_label, selected_choices, index
                )
            )
        return groups


class AsyncAutocompleteSelect(_PreloadedAutocompleteMixin, AutocompleteSelect):
    pass


class AsyncAutocompleteSelectMultiple(
    _PreloadedAutocompleteMixin, AutocompleteSelectMultiple
):
    pass


class AsyncForeignKeyRawIdWidget(ForeignKeyRawIdWidget):
    needs_selected_rows_only = True
    _async_rows = None

    def label_and_url_for_value(self, value):
        key = self.rel.get_related_field().name
        obj = None
        for row in self._async_rows or ():
            if str(getattr(row, key)) == str(value):
                obj = row
                break
        if obj is None:
            return "", ""
        try:
            url = reverse(
                "%s:%s_%s_change"
                % (
                    self.admin_site.name,
                    obj._meta.app_label,
                    obj._meta.model_name,
                ),
                args=(obj.pk,),
            )
        except NoReverseMatch:
            url = ""
        return Truncator(obj).words(14), url


class AsyncManyToManyRawIdWidget(ManyToManyRawIdWidget):
    needs_selected_rows_only = True
