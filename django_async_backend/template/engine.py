"""An async rendering path for Django's template language.

Django's ``Template.render()`` is synchronous all the way down. Under this
backend that means a template cannot reach for anything lazy: an unfetched
foreign key, a related manager, an async queryset or an async method raises
``SynchronousOnlyOperation`` (or ``TypeError``) during rendering.

``arender_template()`` walks the same compiled node tree with ``await``
available at every point where the sync engine evaluates something:

* variable lookups: a lazy relation or deferred field is loaded with
  ``afetch()``, a related manager is swapped for its ``async_objects``, an
  async queryset is evaluated, a coroutine is awaited, and ``count``/``exists``
  /``first``/``last`` on async querysets map to their ``a``-twins;
* ``{% for %}``, ``{% if %}``, ``{% with %}``, ``{% block %}``/``{% extends %}``,
  ``{% include %}``, ``{% autoescape %}``, ``{% spaceless %}``, ``{% filter %}``
  and ``{% ifchanged %}`` have async implementations;
* ``simple_tag`` / ``inclusion_tag`` functions may be ``async def``;
* context processors may be ``async def``.

Any other node renders through Django's own ``render()`` after its
expressions have been resolved asynchronously (see ``patch.py``), which
covers every tag that does not introduce variables for a child nodelist.
"""

import inspect
import warnings
from contextlib import asynccontextmanager

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.exceptions import SynchronousOnlyOperation
from django.db.models.manager import BaseManager
from django.template import (
    defaulttags,
    loader_tags,
)
from django.template.base import (
    FilterExpression,
    NodeList,
    TextNode,
    Variable,
    VariableDoesNotExist,
    VariableNode,
    render_value_in_context,
)
from django.template.context import BaseContext
from django.template.library import (
    InclusionNode,
    SimpleBlockNode,
    SimpleNode,
)
from django.template.loader_tags import (
    BLOCK_CONTEXT_KEY,
    BlockContext,
)
from django.utils.html import conditional_escape
from django.utils.safestring import (
    SafeData,
    SafeString,
    mark_safe,
)
from django.utils.timezone import template_localtime
from django.utils.translation import (
    gettext_lazy,
    pgettext_lazy,
)

from django_async_backend.db.models.convert import (
    is_sync_queryset,
    to_async_queryset,
)
from django_async_backend.db.models.query import QuerySet as AsyncQuerySet

PRERESOLVED_KEY = "__async_template_preresolved"

# Sync queryset/manager method names that templates call without arguments,
# mapped onto the async querysets' a-prefixed twins.
_ASYNC_ALIASES = {"count", "exists", "first", "last", "earliest", "latest"}


# ------------------------------------------------------------ variables ----


def _store(context):
    """The per-render dict of pre-resolved expressions (keyed by id)."""
    render_context = getattr(context, "render_context", None)
    if render_context is None:
        return None
    return render_context.dicts[0].setdefault(PRERESOLVED_KEY, {})


def _is_async_queryset(obj):
    return isinstance(obj, AsyncQuerySet)


def _is_async_manager(obj):
    return isinstance(obj, BaseManager) and issubclass(
        getattr(obj, "_queryset_class", type(None)), AsyncQuerySet
    )


async def _agetattr(current, bit):
    if (_is_async_queryset(current) or _is_async_manager(current)) and (
        bit in _ASYNC_ALIASES
    ):
        bit = "a" + bit
    try:
        return getattr(current, bit)
    except SynchronousOnlyOperation:
        # A lazy relation or deferred field: load it on the async
        # connection and fill Django's cache so the next access is free.
        if hasattr(current, "afetch"):
            return await current.afetch(bit)
        raise


async def _acall(current, context):
    """Mirror of the callable handling in ``Variable._resolve_lookup``."""
    if callable(current):
        if getattr(current, "do_not_call_in_templates", False):
            return current
        if getattr(current, "alters_data", False):
            return context.template.engine.string_if_invalid
        try:
            current = current()
        except TypeError:
            try:
                current_signature = inspect.signature(current)
            except ValueError:
                return context.template.engine.string_if_invalid
            try:
                current_signature.bind()
            except TypeError:
                return context.template.engine.string_if_invalid
            raise
    if inspect.isawaitable(current):
        current = await current
    return current


def _adapt(current):
    """Prefer the async side of what the sync ORM would hand back."""
    if isinstance(current, BaseManager) and not _is_async_manager(current):
        async_objects = getattr(current, "async_objects", None)
        if async_objects is not None:
            return async_objects
    if is_sync_queryset(current):
        # Django's own __aiter__ would run it on a worker thread.
        return to_async_queryset(current)
    return current


async def _aresolve_lookup(variable, context):
    current = context
    try:
        for bit in variable.lookups:
            try:  # dictionary lookup
                if not hasattr(type(current), "__getitem__"):
                    raise TypeError
                current = current[bit]
            except (
                TypeError,
                AttributeError,
                KeyError,
                ValueError,
                IndexError,
            ):
                try:  # attribute lookup
                    if isinstance(current, BaseContext) and getattr(
                        type(current), bit
                    ):
                        raise AttributeError
                    current = await _agetattr(current, bit)
                except (TypeError, AttributeError):
                    if not isinstance(current, BaseContext) and bit in dir(
                        current
                    ):
                        raise
                    try:  # list-index lookup
                        current = current[int(bit)]
                    except (IndexError, ValueError, KeyError, TypeError):
                        raise VariableDoesNotExist(
                            "Failed lookup for key [%s] in %r", (bit, current)
                        )
            current = await _acall(current, context)
            current = _adapt(current)
        if _is_async_queryset(current):
            # A queryset at the end of a lookup is about to be iterated,
            # measured or printed: evaluate it here, on this connection.
            current = [obj async for obj in current]
    except Exception as e:
        if getattr(e, "silent_variable_failure", False):
            current = context.template.engine.string_if_invalid
        else:
            raise
    return current


async def aresolve_variable(variable, context):
    """Async ``Variable.resolve()``."""
    if variable.lookups is not None:
        value = await _aresolve_lookup(variable, context)
    else:
        value = variable.literal
    if variable.translate:
        is_safe = isinstance(value, SafeData)
        msgid = value.replace("%", "%%")
        msgid = mark_safe(msgid) if is_safe else msgid
        if variable.message_context:
            return pgettext_lazy(variable.message_context, msgid)
        return gettext_lazy(msgid)
    return value


async def aresolve(expression, context, ignore_failures=False):
    """Async ``FilterExpression.resolve()``; also accepts a ``Variable``."""
    if isinstance(expression, Variable):
        return await aresolve_variable(expression, context)
    if expression.is_var:
        try:
            obj = await aresolve_variable(expression.var, context)
        except VariableDoesNotExist:
            if ignore_failures:
                obj = None
            else:
                string_if_invalid = context.template.engine.string_if_invalid
                if string_if_invalid:
                    if "%s" in string_if_invalid:
                        return string_if_invalid % expression.var
                    return string_if_invalid
                obj = string_if_invalid
    else:
        obj = expression.var
    for func, args in expression.filters:
        arg_vals = []
        for lookup, arg in args:
            if not lookup:
                arg_vals.append(mark_safe(arg))
            else:
                arg_vals.append(await aresolve_variable(arg, context))
        if getattr(func, "expects_localtime", False):
            obj = template_localtime(obj, context.use_tz)
        if getattr(func, "needs_autoescape", False):
            new_obj = func(obj, autoescape=context.autoescape, *arg_vals)
        else:
            new_obj = func(obj, *arg_vals)
        if inspect.isawaitable(new_obj):
            new_obj = await new_obj
        if getattr(func, "is_safe", False) and isinstance(obj, SafeData):
            obj = mark_safe(new_obj)
        else:
            obj = new_obj
    return obj


# --------------------------------------------------- generic pre-resolve ----


def _iter_expressions(value, seen):
    """Yield every FilterExpression/Variable reachable from a node attribute,
    without descending into child nodelists (they render separately)."""
    if id(value) in seen:
        return
    seen.add(id(value))
    if isinstance(value, (FilterExpression, Variable)):
        yield value
    elif isinstance(value, NodeList):
        return
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _iter_expressions(item, seen)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_expressions(item, seen)
    elif hasattr(value, "__dict__") and hasattr(value, "render"):
        # A nested Node (e.g. the VariableNodes inside blocktranslate).
        for name, attr in vars(value).items():
            if name in getattr(value, "child_nodelists", ()):
                continue
            yield from _iter_expressions(attr, seen)


async def _apreresolve_node(node, context):
    """Resolve a node's expressions asynchronously into the per-render store
    so Django's sync ``render()`` finds them. Returns the ids to clear."""
    store = _store(context)
    ids = []
    seen = set()
    for name, attr in vars(node).items():
        if name in getattr(node, "child_nodelists", ()):
            continue
        for expr in _iter_expressions(attr, seen):
            try:
                value = await aresolve(expr, context, ignore_failures=True)
            except Exception:
                # Let the sync resolve raise it in Django's own way.
                continue
            store[id(expr)] = value
            ids.append(id(expr))
    return ids


def _clear(context, ids):
    store = _store(context)
    for i in ids:
        store.pop(i, None)


class AsyncTemplateHopWarning(RuntimeWarning):
    """A template node queried the sync ORM and was rendered in a thread."""


_hopped_node_types = set()


async def _hop_sync_node(node, fn, *args, **kwargs):
    """A node's own code queried the sync ORM (a third-party tag without an
    ``arender()``). Policy ``ASYNC_BACKEND_TEMPLATE_SYNC_NODES``: ``"hop"``
    (default) runs it in a worker thread on the sync connection and warns
    once per node class; ``"error"`` re-raises ``SynchronousOnlyOperation``.
    """
    policy = getattr(settings, "ASYNC_BACKEND_TEMPLATE_SYNC_NODES", "hop")
    if policy != "hop":
        raise
    key = type(node)
    label = getattr(getattr(node, "func", None), "__name__", None) or key.__qualname__
    if key not in _hopped_node_types:
        _hopped_node_types.add(key)
        warnings.warn(
            "Template node %s.%s (%s) queries the database synchronously; it "
            "was run in a worker thread on the sync connection. Make the tag "
            "`async def`, give the node an `async def arender(self, context)`, "
            "or set ASYNC_BACKEND_TEMPLATE_SYNC_NODES = 'error'."
            % (key.__module__, key.__qualname__, label),
            AsyncTemplateHopWarning,
            stacklevel=3,
        )
    return await sync_to_async(fn)(*args, **kwargs)


async def _arender_fallback(node, context):
    ids = await _apreresolve_node(node, context)
    try:
        return node.render(context)
    except SynchronousOnlyOperation:
        return await _hop_sync_node(node, node.render, context)
    finally:
        _clear(context, ids)


# ------------------------------------------------------------------ nodes ----


async def arender_nodelist(nodelist, context):
    parts = []
    for node in nodelist:
        parts.append(await arender_annotated(node, context))
    return SafeString("".join(parts))


async def arender_annotated(node, context):
    """Mirror of ``Node.render_annotated()``."""
    try:
        return await arender_node(node, context)
    except Exception as e:
        if context.template.engine.debug:
            if not hasattr(e, "_culprit_node"):
                e._culprit_node = node
            if (
                not hasattr(e, "template_debug")
                and context.render_context.template.origin
                == e._culprit_node.origin
            ):
                e.template_debug = (
                    context.render_context.template.get_exception_info(
                        e, e._culprit_node.token
                    )
                )
        raise


async def arender_node(node, context):
    # Third-party nodes opt in by defining ``async def arender(context)``.
    arender = getattr(node, "arender", None)
    if arender is not None and inspect.iscoroutinefunction(arender):
        return await arender(context)
    for klass, renderer in _RENDERERS:
        if isinstance(node, klass):
            return await renderer(node, context)
    return await _arender_fallback(node, context)


async def _text(node, context):
    return node.s


async def _variable(node, context):
    try:
        output = await aresolve(node.filter_expression, context)
    except UnicodeDecodeError:
        return ""
    return render_value_in_context(output, context)


async def _for(node, context):
    parentloop = context["forloop"] if "forloop" in context else {}
    with context.push():
        values = await aresolve(node.sequence, context, ignore_failures=True)
        if values is None:
            values = []
        if hasattr(values, "__aiter__") and not isinstance(
            values, (list, tuple)
        ):
            values = [v async for v in values]
        if not hasattr(values, "__len__"):
            values = list(values)
        len_values = len(values)
        if len_values < 1:
            return await arender_nodelist(node.nodelist_empty, context)
        parts = []
        if node.is_reversed:
            values = reversed(values)
        num_loopvars = len(node.loopvars)
        unpack = num_loopvars > 1
        loop_dict = context["forloop"] = {
            "parentloop": parentloop,
            "length": len_values,
        }
        for i, item in enumerate(values):
            loop_dict["counter0"] = i
            loop_dict["counter"] = i + 1
            loop_dict["revcounter"] = len_values - i
            loop_dict["revcounter0"] = len_values - i - 1
            loop_dict["first"] = i == 0
            loop_dict["last"] = i == len_values - 1
            pop_context = False
            if unpack:
                try:
                    len_item = len(item)
                except TypeError:
                    len_item = 1
                if num_loopvars != len_item:
                    raise ValueError(
                        "Need {} values to unpack in for loop; got {}. ".format(
                            num_loopvars, len_item
                        ),
                    )
                unpacked_vars = dict(zip(node.loopvars, item))
                pop_context = True
                context.update(unpacked_vars)
            else:
                context[node.loopvars[0]] = item
            for child in node.nodelist_loop:
                parts.append(await arender_annotated(child, context))
            if pop_context:
                context.pop()
    return mark_safe("".join(parts))


def _condition_literals(condition):
    if condition is None:
        return
    if isinstance(condition, defaulttags.TemplateLiteral):
        yield condition
        return
    for attr in ("first", "second"):
        child = getattr(condition, attr, None)
        if child is not None and hasattr(child, "eval"):
            yield from _condition_literals(child)


async def _if(node, context):
    for condition, nodelist in node.conditions_nodelists:
        if condition is not None:
            ids = []
            store = _store(context)
            for literal in _condition_literals(condition):
                try:
                    value = await aresolve(
                        literal.value, context, ignore_failures=True
                    )
                except VariableDoesNotExist:
                    continue
                store[id(literal.value)] = value
                ids.append(id(literal.value))
            try:
                match = condition.eval(context)
            except VariableDoesNotExist:
                match = None
            finally:
                _clear(context, ids)
        else:
            match = True
        if match:
            return await arender_nodelist(nodelist, context)
    return ""


async def _with(node, context):
    values = {}
    for key, val in node.extra_context.items():
        values[key] = await aresolve(val, context)
    with context.push(**values):
        return await arender_nodelist(node.nodelist, context)


async def _autoescape(node, context):
    old_setting = context.autoescape
    context.autoescape = node.setting
    output = await arender_nodelist(node.nodelist, context)
    context.autoescape = old_setting
    if node.setting:
        return mark_safe(output)
    return output


async def _spaceless(node, context):
    from django.utils.html import strip_spaces_between_tags

    return strip_spaces_between_tags(
        (await arender_nodelist(node.nodelist, context)).strip()
    )


async def _filter(node, context):
    output = await arender_nodelist(node.nodelist, context)
    with context.push(var=output):
        return await aresolve(node.filter_expr, context)


async def _ifchanged(node, context):
    state_frame = node._get_context_stack_frame(context)
    state_frame.setdefault(node)
    nodelist_true_output = None
    if node._varlist:
        compare_to = [
            await aresolve(var, context, ignore_failures=True)
            for var in node._varlist
        ]
    else:
        compare_to = nodelist_true_output = await arender_nodelist(
            node.nodelist_true, context
        )
    if compare_to != state_frame[node]:
        state_frame[node] = compare_to
        return nodelist_true_output or await arender_nodelist(
            node.nodelist_true, context
        )
    elif node.nodelist_false:
        return await arender_nodelist(node.nodelist_false, context)
    return ""


class _AsyncBlock:
    """``block`` context variable with an awaitable ``super``."""

    do_not_call_in_templates = True

    def __init__(self, block):
        self._block = block

    def __getattr__(self, name):
        return getattr(self._block, name)

    async def super(self):
        block = self._block
        if not hasattr(block, "context"):
            from django.template import TemplateSyntaxError

            raise TemplateSyntaxError(
                "'%s' object has no attribute 'context'. Did you use "
                "{{ block.super }} in a base template?" % type(block).__name__
            )
        render_context = block.context.render_context
        if (
            BLOCK_CONTEXT_KEY in render_context
            and render_context[BLOCK_CONTEXT_KEY].get_block(block.name)
            is not None
        ):
            return mark_safe(await _block(block, block.context))
        return ""


async def _block(node, context):
    block_context = context.render_context.get(BLOCK_CONTEXT_KEY)
    with context.push():
        if block_context is None:
            context["block"] = _AsyncBlock(node)
            result = await arender_nodelist(node.nodelist, context)
        else:
            push = block = block_context.pop(node.name)
            if block is None:
                block = node
            block = type(node)(block.name, block.nodelist)
            block.context = context
            context["block"] = _AsyncBlock(block)
            result = await arender_nodelist(block.nodelist, context)
            if push is not None:
                block_context.push(node.name, push)
    return result


async def _extends(node, context):
    compiled_parent = node.get_parent(context)
    if BLOCK_CONTEXT_KEY not in context.render_context:
        context.render_context[BLOCK_CONTEXT_KEY] = BlockContext()
    block_context = context.render_context[BLOCK_CONTEXT_KEY]
    block_context.add_blocks(node.blocks)
    for child in compiled_parent.nodelist:
        if not isinstance(child, TextNode):
            if not isinstance(child, loader_tags.ExtendsNode):
                blocks = {
                    n.name: n
                    for n in compiled_parent.nodelist.get_nodes_by_type(
                        loader_tags.BlockNode
                    )
                }
                block_context.add_blocks(blocks)
            break
    with context.render_context.push_state(
        compiled_parent, isolated_context=False
    ):
        return await arender_nodelist(compiled_parent.nodelist, context)


async def _include(node, context):
    template = await aresolve(node.template, context)
    if not callable(getattr(template, "render", None)):
        template_name = template or ()
        if isinstance(template_name, str):
            template_name = (
                loader_tags.construct_relative_path(
                    node.origin.template_name, template_name
                ),
            )
        else:
            template_name = tuple(template_name)
        cache = context.render_context.dicts[0].setdefault(node, {})
        template = cache.get(template_name)
        if template is None:
            template = context.template.engine.select_template(template_name)
            cache[template_name] = template
    elif hasattr(template, "template"):
        template = template.template
    values = {}
    for name, var in node.extra_context.items():
        values[name] = await aresolve(var, context)
    if node.isolated_context:
        return await arender_template(template, context.new(values))
    with context.push(**values):
        return await arender_template(template, context)


async def _resolved_arguments(node, context, block_content=None):
    resolved_args = [await aresolve(var, context) for var in node.args]
    if block_content is not None:
        resolved_args.insert(0, block_content)
    if node.takes_context:
        resolved_args = [context, *resolved_args]
    resolved_kwargs = {}
    for k, v in node.kwargs.items():
        resolved_kwargs[k] = await aresolve(v, context)
    return resolved_args, resolved_kwargs


async def _simple(node, context):
    block_content = None
    if isinstance(node, SimpleBlockNode):
        block_content = await arender_nodelist(node.nodelist, context)
    resolved_args, resolved_kwargs = await _resolved_arguments(
        node, context, block_content
    )
    try:
        output = node.func(*resolved_args, **resolved_kwargs)
    except SynchronousOnlyOperation:
        output = await _hop_sync_node(
            node, node.func, *resolved_args, **resolved_kwargs
        )
    if inspect.isawaitable(output):
        output = await output
    if node.target_var is not None:
        context[node.target_var] = output
        return ""
    if context.autoescape:
        output = conditional_escape(output)
    return output


async def _inclusion(node, context):
    from collections.abc import Iterable

    from django.template.base import Template

    resolved_args, resolved_kwargs = await _resolved_arguments(node, context)
    try:
        _dict = node.func(*resolved_args, **resolved_kwargs)
    except SynchronousOnlyOperation:
        _dict = await _hop_sync_node(
            node, node.func, *resolved_args, **resolved_kwargs
        )
    if inspect.isawaitable(_dict):
        _dict = await _dict
    t = context.render_context.get(node)
    if (
        t is None
        and getattr(node, "template_name", None)
        and node.filename is None
    ):
        # The admin's InclusionAdminNode picks its template per model/app.
        opts = context["opts"]
        app_label = opts.app_label.lower()
        object_name = opts.model_name
        t = context.template.engine.select_template(
            [
                "admin/%s/%s/%s"
                % (app_label, object_name, node.template_name),
                "admin/%s/%s" % (app_label, node.template_name),
                "admin/%s" % node.template_name,
            ]
        )
        context.render_context[node] = t
    if t is None:
        if isinstance(node.filename, Template):
            t = node.filename
        elif isinstance(getattr(node.filename, "template", None), Template):
            t = node.filename.template
        elif not isinstance(node.filename, str) and isinstance(
            node.filename, Iterable
        ):
            t = context.template.engine.select_template(node.filename)
        else:
            t = context.template.engine.get_template(node.filename)
        context.render_context[node] = t
    new_context = context.new(_dict)
    csrf_token = context.get("csrf_token")
    if csrf_token is not None:
        new_context["csrf_token"] = csrf_token
    return await arender_template(t, new_context)


# Order matters: subclasses before their bases.
async def _cache_tag(node, context):
    """``{% cache %}`` awaiting the fragment cache (``CacheNode``)."""
    from django.core.cache import InvalidCacheBackendError, caches
    from django.core.cache.utils import make_template_fragment_key
    from django.template import TemplateSyntaxError, VariableDoesNotExist

    try:
        expire_time = await aresolve(node.expire_time_var, context)
    except VariableDoesNotExist:
        raise TemplateSyntaxError(
            '"cache" tag got an unknown variable: %r' % node.expire_time_var.var
        )
    if expire_time is not None:
        try:
            expire_time = int(expire_time)
        except (ValueError, TypeError):
            raise TemplateSyntaxError(
                '"cache" tag got a non-integer timeout value: %r' % expire_time
            )
    if node.cache_name:
        try:
            cache_name = await aresolve(node.cache_name, context)
        except VariableDoesNotExist:
            raise TemplateSyntaxError(
                '"cache" tag got an unknown variable: %r' % node.cache_name.var
            )
        try:
            fragment_cache = caches[cache_name]
        except InvalidCacheBackendError:
            raise TemplateSyntaxError(
                "Invalid cache name specified for cache tag: %r" % cache_name
            )
    else:
        try:
            fragment_cache = caches["template_fragments"]
        except InvalidCacheBackendError:
            fragment_cache = caches["default"]
    vary_on = [await aresolve(var, context) for var in node.vary_on]
    cache_key = make_template_fragment_key(node.fragment_name, vary_on)
    value = await fragment_cache.aget(cache_key)
    if value is None:
        value = await arender_nodelist(node.nodelist, context)
        await fragment_cache.aset(cache_key, value, expire_time)
    return value


from django.templatetags.cache import CacheNode  # noqa: E402

_RENDERERS = [
    (CacheNode, _cache_tag),
    (TextNode, _text),
    (VariableNode, _variable),
    (defaulttags.ForNode, _for),
    (defaulttags.IfNode, _if),
    (defaulttags.WithNode, _with),
    (defaulttags.AutoEscapeControlNode, _autoescape),
    (defaulttags.SpacelessNode, _spaceless),
    (defaulttags.FilterNode, _filter),
    (defaulttags.IfChangedNode, _ifchanged),
    (loader_tags.BlockNode, _block),
    (loader_tags.ExtendsNode, _extends),
    (loader_tags.IncludeNode, _include),
    (SimpleNode, _simple),  # covers SimpleBlockNode
    (InclusionNode, _inclusion),
]


# -------------------------------------------------------------- template ----


@asynccontextmanager
async def abind_template(context, template):
    """Async ``Context.bind_template()``; context processors may be async."""
    if context.template is not None:
        raise RuntimeError("Context is already bound to a template")
    context.template = template
    processors_index = getattr(context, "_processors_index", None)
    if processors_index is not None:
        processors = (
            template.engine.template_context_processors + context._processors
        )
        updates = {}
        for processor in processors:
            result = processor(context.request)
            if inspect.isawaitable(result):
                result = await result
            try:
                updates.update(result)
            except TypeError as e:
                raise TypeError(
                    f"Context processor {processor.__qualname__} didn't "
                    "return a dictionary."
                ) from e
        context.dicts[processors_index] = updates
    try:
        yield
    finally:
        context.template = None
        if processors_index is not None:
            context.dicts[processors_index] = {}


async def arender_template(template, context):
    """Async ``django.template.base.Template.render()``."""
    with context.render_context.push_state(template):
        if context.template is None:
            async with abind_template(context, template):
                context.template_name = template.name
                return await arender_nodelist(template.nodelist, context)
        return await arender_nodelist(template.nodelist, context)
