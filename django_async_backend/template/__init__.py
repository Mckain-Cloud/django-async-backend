"""Async rendering of Django templates.

::

    from django_async_backend.template import arender_to_string

    html = await arender_to_string("book.html", {"book": book}, request)

Anything lazy the template touches (unfetched relations, related managers,
async querysets, async model methods, async simple/inclusion tags, async
context processors) is awaited on the request's async connection. Enabled
on startup unless ``ASYNC_BACKEND_ASYNC_TEMPLATES = False``.
"""

from django.template import engines
from django.template.backends.django import Template as DjangoBackendTemplate
from django.template.base import Template as BaseTemplate
from django.template.context import make_context
from django.template.loader import (
    get_template,
    select_template,
)

from django_async_backend.template.engine import (
    arender_nodelist,
    arender_template,
    aresolve,
    aresolve_variable,
)

__all__ = [
    "arender",
    "arender_response",
    "arender_to_string",
    "arender_template",
    "arender_nodelist",
    "aresolve",
    "aresolve_variable",
]


def _unwrap(template):
    if isinstance(template, BaseTemplate):
        return template, None
    if isinstance(template, DjangoBackendTemplate):
        return template.template, template.backend
    inner = getattr(template, "template", None)
    if isinstance(inner, BaseTemplate):
        return inner, getattr(template, "backend", None)
    raise TypeError(
        "arender() needs a Django template; %r is from another backend."
        % type(template).__name__
    )


async def arender(template, context=None, request=None):
    """Async equivalent of ``template.render(context, request)``."""
    base, backend = _unwrap(template)
    if backend is not None:
        autoescape = backend.engine.autoescape
    else:
        autoescape = base.engine.autoescape
    context = make_context(context, request, autoescape=autoescape)
    return await arender_template(base, context)


async def arender_to_string(
    template_name, context=None, request=None, using=None
):
    """Async ``django.template.loader.render_to_string()``."""
    if isinstance(template_name, (list, tuple)):
        template = select_template(template_name, using=using)
    else:
        template = get_template(template_name, using=using)
    return await arender(template, context, request)


async def arender_response(response):
    """Render a ``SimpleTemplateResponse`` asynchronously, in place.

    Equivalent to ``response.render()`` but the template goes through
    ``arender()``. Returns the (possibly replaced) response, like
    ``render()`` does.
    """
    if getattr(response, "is_rendered", True):
        return response
    template = response.resolve_template(response.template_name)
    context = response.resolve_context(response.context_data)
    request = getattr(response, "_request", None)
    response.content = await arender(template, context, request)
    for post_callback in response._post_render_callbacks:
        newretval = post_callback(response)
        if newretval is not None:
            response = newretval
    return response


def default_engine():
    return engines["django"]
