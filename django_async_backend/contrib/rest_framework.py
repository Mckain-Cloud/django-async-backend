"""Django REST framework on the async connection.

Built on `adrf <https://github.com/em1208/adrf>`_, which supplies async
views, viewsets and routers but still reaches the database through
``sync_to_async`` (authentication, ``is_valid()``, pagination counts,
``PrimaryKeyRelatedField`` lookups, ``_default_manager.acreate()``). The
classes here replace each of those hops with the async ORM of this package,
so a request is served entirely on its task's connection:

* ``AsyncModelSerializer`` (``ais_valid()``, ``adata``, ``asave()``) with
  ``AsyncPrimaryKeyRelatedField`` / ``AsyncSlugRelatedField`` lookups,
  ``AsyncUniqueValidator`` / ``AsyncUniqueTogetherValidator``, relations
  loaded with ``afetch()`` before representation, ``acreate()`` /
  ``aupdate()`` on ``async_objects`` and ``async_save()``.
  Hooks: ``avalidate_<field>()``, ``avalidate()``, ``aget_<method field>()``.
* ``AsyncAPIView`` whose ``ainitial()`` authenticates with
  ``aauthenticate()`` (``AsyncSessionAuthentication`` uses
  ``request.auser()``), warms the user's permission cache so
  ``DjangoModelPermissions`` runs no query, and checks permissions and
  throttles in the request's task (adrf gathers them in separate tasks,
  which cannot share the connection).
* ``AsyncGenericAPIView`` (``aget_object()`` via ``aget()``,
  ``apaginate_queryset()``), the generic views, mixins,
  ``AsyncModelViewSet`` / ``AsyncReadOnlyModelViewSet`` and
  ``AsyncPageNumberPagination`` / ``AsyncLimitOffsetPagination``.

Register viewsets with adrf's ``DefaultRouter``/``SimpleRouter`` (re-exported
here); they map the ``alist``/``acreate``/... actions.
"""

import base64
import binascii
from collections.abc import Mapping
from inspect import iscoroutinefunction

from asgiref.sync import sync_to_async
from django.contrib.auth import aauthenticate, get_user_model
from django.core.exceptions import FieldDoesNotExist, ObjectDoesNotExist
from django.core.exceptions import SynchronousOnlyOperation
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.paginator import InvalidPage
from django.db import DataError
from django.db.models import Exists
from django.db.models import Model
from django.db.models import QuerySet as DjangoQuerySet
from django.db.models.fields.related import RelatedField as ModelRelatedField
from django.db.models.manager import BaseManager
from django.http import Http404
from django.utils.translation import gettext_lazy as _
from rest_framework import authentication, exceptions, pagination, permissions
from rest_framework import relations, status, throttling
from rest_framework.exceptions import ValidationError
from rest_framework.fields import SerializerMethodField, SkipField, empty
from rest_framework.fields import get_error_detail
from rest_framework.relations import MANY_RELATION_KWARGS, PKOnlyObject
from rest_framework.response import Response
from rest_framework.serializers import (
    BaseSerializer,
    as_serializer_error,
    raise_errors_on_nested_writes,
)
from rest_framework.settings import api_settings
from rest_framework.utils import model_meta
from rest_framework.utils.serializer_helpers import ReturnDict, ReturnList
from rest_framework.validators import UniqueTogetherValidator, UniqueValidator
from rest_framework.validators import _check_single_instance, qs_filter

from adrf import generics as adrf_generics
from adrf import mixins as adrf_mixins
from adrf import serializers as adrf_serializers
from adrf import views as adrf_views
from adrf import viewsets as adrf_viewsets
from adrf.requests import AsyncRequest as AdrfAsyncRequest
from adrf.routers import DefaultRouter, SimpleRouter  # noqa: F401 (re-export)
from django_async_backend.contrib.auth import awarm_permissions
from django_async_backend.db.models.constraints import aq_check
from django_async_backend.db.models.convert import (
    is_sync_queryset,
    to_async_queryset,
)
from django_async_backend.db.models.query import QuerySet as AsyncQuerySet

__all__ = [
    "AsyncAPIView",
    "AsyncBasicAuthentication",
    "AsyncDjangoModelPermissions",
    "AsyncDjangoObjectPermissions",
    "AsyncRemoteUserAuthentication",
    "AsyncTokenAuthentication",
    "AsyncAnonRateThrottle",
    "AsyncScopedRateThrottle",
    "AsyncSimpleRateThrottle",
    "AsyncUserRateThrottle",
    "AsyncCreateAPIView",
    "AsyncDestroyAPIView",
    "AsyncGenericAPIView",
    "AsyncGenericViewSet",
    "AsyncLimitOffsetPagination",
    "AsyncListAPIView",
    "AsyncListCreateAPIView",
    "AsyncListSerializer",
    "AsyncManyRelatedField",
    "AsyncModelSerializer",
    "AsyncModelViewSet",
    "AsyncPageNumberPagination",
    "AsyncPrimaryKeyRelatedField",
    "AsyncReadOnlyModelViewSet",
    "AsyncRequest",
    "AsyncRetrieveAPIView",
    "AsyncRetrieveDestroyAPIView",
    "AsyncRetrieveUpdateAPIView",
    "AsyncRetrieveUpdateDestroyAPIView",
    "AsyncSerializer",
    "AsyncSessionAuthentication",
    "AsyncSlugRelatedField",
    "AsyncUniqueTogetherValidator",
    "AsyncUniqueValidator",
    "AsyncUpdateAPIView",
    "DefaultRouter",
    "SimpleRouter",
    "to_async",
]


# ---------------------------------------------------------------- helpers ----


def to_async(value):
    """Async queryset for a sync queryset or (any) manager; else unchanged."""
    if isinstance(value, BaseManager):
        value = value.all()
    return to_async_queryset(value)


async def aiterate(data):
    """Rows of a manager / sync or async queryset / iterable, without a sync
    query: cached results are reused, a sync queryset is converted."""
    if isinstance(data, BaseManager):
        data = data.all()
    if isinstance(data, (DjangoQuerySet, AsyncQuerySet)):
        if data._result_cache is not None:
            return list(data._result_cache)
        data = to_async_queryset(data)
        if is_sync_queryset(data):
            raise TypeError(
                "%r has no async twin; evaluate it first." % (data,)
            )
        return [obj async for obj in data]
    return list(data)


async def _aexists(queryset):
    try:
        return await to_async(queryset).aexists()
    except (TypeError, ValueError, DataError):
        return False


async def _aexists_with_condition(queryset, condition, against):
    if condition is None:
        return await _aexists(queryset)
    try:
        return await aq_check(
            condition & Exists(to_async(queryset).filter(condition)), against
        )
    except (TypeError, ValueError, DataError):
        return False


def _relation(instance, attr):
    try:
        field = instance._meta.get_field(attr)
    except FieldDoesNotExist:
        return None
    return field if field.is_relation else None


def _relation_cached(instance, attr, field):
    if field.many_to_many or field.one_to_many:
        return attr in instance.__dict__.get("_prefetched_objects_cache", {})
    is_cached = getattr(field, "is_cached", None)
    return bool(is_cached and is_cached(instance))


async def aload_source(instance, attrs):
    """Load every relation along ``attrs`` on the async connection, so the
    sync attribute walk DRF does afterwards runs no query."""
    for attr in attrs:
        if isinstance(instance, Model):
            field = _relation(instance, attr)
            if field is not None and not _relation_cached(instance, attr, field):
                await instance.afetch(attr)
        try:
            instance = (
                instance[attr] if isinstance(instance, Mapping) else getattr(instance, attr)
            )
        except (KeyError, AttributeError, ObjectDoesNotExist):
            return
        if not isinstance(instance, Model):
            return


def _needs_object(field):
    """Does representing ``field`` need the related object(s) loaded?"""
    if isinstance(field, relations.ManyRelatedField):
        return True
    if isinstance(field, relations.RelatedField):
        return not field.use_pk_only_optimization()
    return isinstance(field, BaseSerializer)


async def arun_field_validation(field, data):
    """``field.run_validation(data)`` with async lookups and validators."""
    arun = getattr(field, "arun_validation", None)
    if iscoroutinefunction(arun):
        return await arun(data)
    async_validators = [
        v for v in field.validators if iscoroutinefunction(getattr(v, "acall", None))
    ]
    if not async_validators:
        return field.run_validation(data)
    saved = field.validators
    field.validators = [v for v in saved if v not in async_validators]
    try:
        value = field.run_validation(data)
    finally:
        field.validators = saved
    if value is not empty:
        for validator in async_validators:
            await validator.acall(value, field)
    return value


async def ais_valid(serializer, *, raise_exception=False):
    """``serializer.ais_valid()`` when available, else a thread hop."""
    method = getattr(serializer, "ais_valid", None)
    if iscoroutinefunction(method):
        return await method(raise_exception=raise_exception)
    return await sync_to_async(serializer.is_valid)(raise_exception=raise_exception)


# ------------------------------------------------------------- validators ----


class AsyncUniqueValidator(UniqueValidator):
    """``UniqueValidator`` whose check runs through ``acall()`` on the async
    connection; ``__call__`` keeps DRF's sync behaviour for sync callers."""

    @classmethod
    def from_sync(cls, validator):
        return cls(validator.queryset, validator.message, validator.lookup)

    async def acall(self, value, serializer_field):
        field_name = serializer_field.source_attrs[-1]
        serializer = serializer_field.parent
        instance = getattr(serializer, "instance", None)
        _check_single_instance(serializer, instance, self)
        queryset = to_async(self.queryset)
        queryset = self.filter_queryset(value, queryset, field_name)
        queryset = self.exclude_current_instance(queryset, instance)
        if await _aexists(queryset):
            raise ValidationError(self.message, code="unique")


class AsyncUniqueTogetherValidator(UniqueTogetherValidator):
    @classmethod
    def from_sync(cls, v):
        return cls(
            v.queryset,
            v.fields,
            message=v.message,
            condition_fields=v.condition_fields,
            condition=v.condition,
            code=v.code,
            nulls_distinct=v.nulls_distinct,
        )

    async def acall(self, attrs, serializer):
        # Mirrors UniqueTogetherValidator.__call__ with an async EXISTS.
        _check_single_instance(serializer, serializer.instance, self)
        self.enforce_required_fields(attrs, serializer)
        queryset = to_async(self.queryset)
        queryset = self.filter_queryset(attrs, queryset, serializer)
        queryset = self.exclude_current_instance(attrs, queryset, serializer.instance)

        constraint_field_sources = [
            serializer.fields[name].source for name in self.fields
        ]
        constraint_field_values = [
            attrs[source]
            if source in attrs
            else getattr(serializer.instance, source, None)
            for source in constraint_field_sources
        ]
        checked_names = list(
            {serializer.fields[name].source for name in self.fields}
            | {serializer.fields[name].source for name in self.condition_fields}
        )
        if serializer.instance is None:
            checked_values = [attrs.get(name) for name in checked_names]
        else:
            checked_values = [
                attrs[name]
                for name in checked_names
                if name in attrs
                and attrs[name] != getattr(serializer.instance, name, None)
            ]
        condition_sources = (
            serializer.fields[name].source for name in self.condition_fields
        )
        condition_kwargs = {
            source: attrs.get(source)
            if source in attrs
            else getattr(serializer.instance, source, None)
            for source in condition_sources
        }
        if checked_values:
            if self.nulls_distinct is not False and any(
                v is None for v in constraint_field_values
            ):
                return
            if await _aexists_with_condition(
                queryset, self.condition, condition_kwargs
            ):
                field_names = ", ".join(self.fields)
                message = self.message.format(field_names=field_names)
                raise ValidationError(message, code=self.code)


# --------------------------------------------------------- related fields ----


class AsyncManyRelatedField(relations.ManyRelatedField):
    async def arun_validation(self, data=empty):
        (is_empty_value, data) = self.validate_empty_values(data)
        if is_empty_value:
            return data
        value = await self.ato_internal_value(data)
        self.run_validators(value)
        return value

    async def ato_internal_value(self, data):
        if isinstance(data, str) or not hasattr(data, "__iter__"):
            self.fail("not_a_list", input_type=type(data).__name__)
        if not self.allow_empty and len(data) == 0:
            self.fail("empty")
        child = self.child_relation
        convert = getattr(child, "ato_internal_value", None)
        if iscoroutinefunction(convert):
            return [await convert(item) for item in data]
        return [child.to_internal_value(item) for item in data]

    async def ato_representation(self, iterable):
        return [
            self.child_relation.to_representation(value)
            for value in await aiterate(iterable)
        ]


class _AsyncRelatedMixin:
    @classmethod
    def many_init(cls, *args, **kwargs):
        list_kwargs = {"child_relation": cls(*args, **kwargs)}
        for key in kwargs:
            if key in MANY_RELATION_KWARGS:
                list_kwargs[key] = kwargs[key]
        return AsyncManyRelatedField(**list_kwargs)

    def aget_queryset(self):
        return to_async(self.get_queryset())

    async def arun_validation(self, data=empty):
        if data == "":
            data = None
        (is_empty_value, data) = self.validate_empty_values(data)
        if is_empty_value:
            return data
        value = await self.ato_internal_value(data)
        self.run_validators(value)
        return value


class AsyncPrimaryKeyRelatedField(_AsyncRelatedMixin, relations.PrimaryKeyRelatedField):
    async def ato_internal_value(self, data):
        if self.pk_field is not None:
            data = self.pk_field.to_internal_value(data)
        queryset = self.aget_queryset()
        try:
            if isinstance(data, bool):
                raise TypeError
            return await queryset.aget(pk=data)
        except ObjectDoesNotExist:
            self.fail("does_not_exist", pk_value=data)
        except (TypeError, ValueError):
            self.fail("incorrect_type", data_type=type(data).__name__)


class AsyncSlugRelatedField(_AsyncRelatedMixin, relations.SlugRelatedField):
    async def ato_internal_value(self, data):
        queryset = self.aget_queryset()
        try:
            return await queryset.aget(**{self.slug_field: data})
        except ObjectDoesNotExist:
            self.fail(
                "does_not_exist", slug_name=self.slug_field, value=str(data)
            )
        except (TypeError, ValueError):
            self.fail("invalid")


# ------------------------------------------------------------ serializers ----


class _AsyncDataMixin:
    """``adata`` as a plain property returning a coroutine.

    adrf declares ``adata`` with ``async_property``, whose descriptor runs
    ``functools.wraps()`` on the shared ``adata`` function for every access.
    On the free-threaded build, ``update_wrapper`` on an object shared
    between threads serialises them (observed: a thousandfold slowdown with
    two threads), so the property is replaced here; ``await serializer.adata``
    keeps working.
    """

    @property
    def adata(self):
        return self._aget_data()

    async def _aget_data(self):
        if hasattr(self, "initial_data") and not hasattr(self, "_validated_data"):
            raise AssertionError(
                "When a serializer is passed a `data` keyword argument you "
                "must call `.is_valid()` before attempting to access the "
                "serialized `.data` representation.\n"
                "You should either call `.is_valid()` first, "
                "or access `.initial_data` instead."
            )
        if not hasattr(self, "_data"):
            if self.instance is not None and not getattr(self, "_errors", None):
                self._data = await self.ato_representation(self.instance)
            elif hasattr(self, "_validated_data") and not getattr(
                self, "_errors", None
            ):
                self._data = await self.ato_representation(self.validated_data)
            else:
                self._data = self.get_initial()
        return self._wrap_data(self._data)

    def _wrap_data(self, data):
        return ReturnDict(data, serializer=self)


class _AsyncSerializerMixin(_AsyncDataMixin):
    """Async representation and validation for ``Serializer`` subclasses."""

    async def _aget_attribute(self, field, instance):
        if _needs_object(field):
            await aload_source(instance, field.source_attrs)
        aget = getattr(field, "aget_attribute", None)
        if iscoroutinefunction(aget) and not isinstance(field, BaseSerializer):
            return await aget(instance)
        try:
            return field.get_attribute(instance)
        except SynchronousOnlyOperation:
            await aload_source(instance, field.source_attrs)
            return field.get_attribute(instance)

    async def ato_representation(self, instance):
        ret = {}
        for field in self._readable_fields:
            if isinstance(field, SerializerMethodField):
                amethod = getattr(self, "a" + field.method_name, None)
                if iscoroutinefunction(amethod):
                    ret[field.field_name] = await amethod(instance)
                    continue
            try:
                attribute = await self._aget_attribute(field, instance)
            except SkipField:
                continue
            check_for_none = (
                attribute.pk if isinstance(attribute, PKOnlyObject) else attribute
            )
            if check_for_none is None:
                ret[field.field_name] = None
                continue
            arepr = getattr(field, "ato_representation", None)
            if iscoroutinefunction(arepr):
                ret[field.field_name] = await arepr(attribute)
            else:
                ret[field.field_name] = field.to_representation(attribute)
        return ret

    async def ais_valid(self, *, raise_exception=False):
        assert hasattr(self, "initial_data"), (
            "Cannot call `.ais_valid()` as no `data=` keyword argument was "
            "passed when instantiating the serializer instance."
        )
        if not hasattr(self, "_validated_data"):
            try:
                self._validated_data = await self.arun_validation(self.initial_data)
            except ValidationError as exc:
                self._validated_data = {}
                self._errors = exc.detail
            else:
                self._errors = {}
        if self._errors and raise_exception:
            raise ValidationError(self.errors)
        return not bool(self._errors)

    async def arun_validation(self, data=empty):
        (is_empty_value, data) = self.validate_empty_values(data)
        if is_empty_value:
            return data
        value = await self.ato_internal_value(data)
        try:
            await self.arun_validators(value)
            value = self.validate(value)
            assert value is not None, ".validate() should return the validated data"
            avalidate = getattr(self, "avalidate", None)
            if iscoroutinefunction(avalidate):
                value = await avalidate(value)
                assert value is not None, ".avalidate() should return the validated data"
        except (ValidationError, DjangoValidationError) as exc:
            raise ValidationError(detail=as_serializer_error(exc))
        return value

    async def arun_validators(self, value):
        validators = list(self.validators)
        async_validators = [
            v for v in validators if iscoroutinefunction(getattr(v, "acall", None))
        ]
        sync_validators = [v for v in validators if v not in async_validators]
        if sync_validators:
            saved = self.validators
            self.validators = sync_validators
            try:
                self.run_validators(value)
            finally:
                self.validators = saved
        for validator in async_validators:
            await validator.acall(value, self)

    async def ato_internal_value(self, data):
        if not isinstance(data, Mapping):
            message = self.error_messages["invalid"].format(
                datatype=type(data).__name__
            )
            raise ValidationError(
                {api_settings.NON_FIELD_ERRORS_KEY: [message]}, code="invalid"
            )
        ret = {}
        errors = {}
        for field in self._writable_fields:
            validate_method = getattr(self, "validate_" + field.field_name, None)
            avalidate_method = getattr(self, "avalidate_" + field.field_name, None)
            primitive_value = field.get_value(data)
            try:
                validated_value = await arun_field_validation(field, primitive_value)
                if validate_method is not None:
                    validated_value = validate_method(validated_value)
                if iscoroutinefunction(avalidate_method):
                    validated_value = await avalidate_method(validated_value)
            except ValidationError as exc:
                errors[field.field_name] = exc.detail
            except DjangoValidationError as exc:
                errors[field.field_name] = get_error_detail(exc)
            except SkipField:
                pass
            else:
                self.set_value(ret, field.source_attrs, validated_value)
        if errors:
            raise ValidationError(errors)
        return ret


class AsyncListSerializer(_AsyncDataMixin, adrf_serializers.ListSerializer):
    def _wrap_data(self, data):
        return ReturnList(data, serializer=self)

    async def ato_representation(self, data):
        return [
            await self.child.ato_representation(item) for item in await aiterate(data)
        ]


class AsyncSerializer(_AsyncSerializerMixin, adrf_serializers.Serializer):
    class Meta:
        list_serializer_class = AsyncListSerializer


class AsyncModelSerializer(_AsyncSerializerMixin, adrf_serializers.ModelSerializer):
    """``ModelSerializer`` whose lookups, uniqueness checks, relation loading
    and writes use the async connection."""

    serializer_related_field = AsyncPrimaryKeyRelatedField
    serializer_related_to_field = AsyncSlugRelatedField

    @classmethod
    def many_init(cls, *args, **kwargs):
        meta = getattr(cls, "Meta", None)
        if meta is not None and not hasattr(meta, "list_serializer_class"):
            meta.list_serializer_class = AsyncListSerializer
        return super().many_init(*args, **kwargs)

    def get_fields(self):
        fields = super().get_fields()
        for field in fields.values():
            validators = field.validators
            if any(type(v) is UniqueValidator for v in validators):
                field.validators = [
                    AsyncUniqueValidator.from_sync(v) if type(v) is UniqueValidator else v
                    for v in validators
                ]
        return fields

    def get_validators(self):
        return [
            AsyncUniqueTogetherValidator.from_sync(v)
            if type(v) is UniqueTogetherValidator
            else v
            for v in super().get_validators()
        ]

    @property
    def _async_manager(self):
        return self.Meta.model.async_objects

    async def acreate(self, validated_data):
        raise_errors_on_nested_writes("acreate", self, validated_data)
        ModelClass = self.Meta.model
        info = model_meta.get_field_info(ModelClass)
        many_to_many = {}
        for field_name, relation_info in info.relations.items():
            if relation_info.to_many and (field_name in validated_data):
                many_to_many[field_name] = validated_data.pop(field_name)
        try:
            instance = await self._async_manager.acreate(**validated_data)
        except TypeError as exc:
            raise TypeError(
                "Got a `TypeError` when calling `%s.async_objects.acreate()`. "
                "This may be because you have a writable field on the "
                "serializer class that is not a valid argument to "
                "`%s.async_objects.acreate()`. You may need to make the "
                "field read-only, or override the %s.acreate() method to "
                "handle this correctly.\nOriginal exception was:\n %s"
                % (ModelClass.__name__, ModelClass.__name__, type(self).__name__, exc)
            ) from exc
        for field_name, value in many_to_many.items():
            await getattr(instance, field_name).async_objects.aset(value)
        return instance

    async def aupdate(self, instance, validated_data):
        raise_errors_on_nested_writes("aupdate", self, validated_data)
        info = model_meta.get_field_info(instance)
        m2m_fields = []
        for attr, value in validated_data.items():
            if attr in info.relations and info.relations[attr].to_many:
                m2m_fields.append((attr, value))
            else:
                setattr(instance, attr, value)
        await instance.async_save()
        for attr, value in m2m_fields:
            await getattr(instance, attr).async_objects.aset(value)
        return instance


# ------------------------------------------------------- authentication ----


class AsyncSessionAuthentication(authentication.SessionAuthentication):
    async def aauthenticate(self, request):
        django_request = request._request
        auser = getattr(django_request, "auser", None)
        if auser is None:
            return None
        user = await auser()
        if not user or not user.is_active:
            return None
        self.enforce_csrf(request)
        return (user, None)


class AsyncBasicAuthentication(authentication.BasicAuthentication):
    async def aauthenticate(self, request):
        auth = authentication.get_authorization_header(request).split()
        if not auth or auth[0].lower() != b"basic":
            return None
        if len(auth) == 1:
            raise exceptions.AuthenticationFailed(
                _("Invalid basic header. No credentials provided.")
            )
        if len(auth) > 2:
            raise exceptions.AuthenticationFailed(
                _("Invalid basic header. Credentials string should not contain spaces.")
            )
        try:
            try:
                decoded = base64.b64decode(auth[1]).decode("utf-8")
            except UnicodeDecodeError:
                decoded = base64.b64decode(auth[1]).decode("latin-1")
            userid, password = decoded.split(":", 1)
        except (TypeError, ValueError, UnicodeDecodeError, binascii.Error):
            raise exceptions.AuthenticationFailed(
                _("Invalid basic header. Credentials not correctly base64 encoded.")
            )
        return await self.aauthenticate_credentials(userid, password, request)

    async def aauthenticate_credentials(self, userid, password, request=None):
        credentials = {
            get_user_model().USERNAME_FIELD: userid,
            "password": password,
        }
        user = await aauthenticate(request=request, **credentials)
        if user is None:
            raise exceptions.AuthenticationFailed(_("Invalid username/password."))
        if not user.is_active:
            raise exceptions.AuthenticationFailed(_("User inactive or deleted."))
        return (user, None)


class AsyncTokenAuthentication(authentication.TokenAuthentication):
    """``TokenAuthentication`` looking the token up with ``aget()``.

    Requires ``rest_framework.authtoken`` in ``INSTALLED_APPS``.
    """

    async def aauthenticate(self, request):
        auth = authentication.get_authorization_header(request).split()
        if not auth or auth[0].lower() != self.keyword.lower().encode():
            return None
        if len(auth) == 1:
            raise exceptions.AuthenticationFailed(
                _("Invalid token header. No credentials provided.")
            )
        if len(auth) > 2:
            raise exceptions.AuthenticationFailed(
                _("Invalid token header. Token string should not contain spaces.")
            )
        try:
            token = auth[1].decode()
        except UnicodeError:
            raise exceptions.AuthenticationFailed(
                _("Invalid token header. Token string should not contain invalid characters.")
            )
        return await self.aauthenticate_credentials(token)

    async def aauthenticate_credentials(self, key):
        model = self.get_model()
        try:
            token = await model.async_objects.select_related("user").aget(key=key)
        except model.DoesNotExist:
            raise exceptions.AuthenticationFailed(_("Invalid token."))
        if not token.user.is_active:
            raise exceptions.AuthenticationFailed(_("User inactive or deleted."))
        return (token.user, token)


class AsyncRemoteUserAuthentication(authentication.RemoteUserAuthentication):
    async def aauthenticate(self, request):
        auser = getattr(request._request, "auser", None)
        user = await auser() if auser is not None else getattr(request._request, "user", None)
        if user and user.is_active:
            return (user, None)
        return None


class AsyncDjangoModelPermissions(permissions.DjangoModelPermissions):
    """``DjangoModelPermissions`` through ``user.ahas_perms()``."""

    async def has_permission(self, request, view):
        if not request.user or (
            not request.user.is_authenticated and self.authenticated_users_only
        ):
            return False
        if getattr(view, "_ignore_model_permissions", False):
            return True
        queryset = self._queryset(view)
        perms = self.get_required_permissions(request.method, queryset.model)
        return await _ahas_perms(request.user, perms)


class AsyncDjangoObjectPermissions(permissions.DjangoObjectPermissions):
    """``DjangoObjectPermissions`` through ``user.ahas_perms(perms, obj)``.

    An object-permission backend implementing ``ahas_perm()`` runs on the
    request's connection; one with only ``has_perm()`` is reached through
    Django's thread hop.
    """

    async def has_permission(self, request, view):
        if not request.user or (
            not request.user.is_authenticated and self.authenticated_users_only
        ):
            return False
        if getattr(view, "_ignore_model_permissions", False):
            return True
        queryset = self._queryset(view)
        perms = self.get_required_permissions(request.method, queryset.model)
        return await _ahas_perms(request.user, perms)

    async def has_object_permission(self, request, view, obj):
        queryset = self._queryset(view)
        model_cls = queryset.model
        user = request.user
        perms = self.get_required_object_permissions(request.method, model_cls)
        if not await _ahas_perms(user, perms, obj):
            if request.method in permissions.SAFE_METHODS:
                raise Http404
            read_perms = self.get_required_object_permissions("GET", model_cls)
            if not await _ahas_perms(user, read_perms, obj):
                raise Http404
            return False
        return True


async def _ahas_perms(user, perms, obj=None):
    ahas = getattr(user, "ahas_perms", None)
    if ahas is not None:
        return await ahas(perms, obj)
    return await sync_to_async(user.has_perms)(perms, obj)


class AsyncRequest(AdrfAsyncRequest):
    async def aauthenticate(self):
        """Authenticate once, preferring ``aauthenticate()`` on each
        authenticator; sync ones that touch the database hop to a thread."""
        if hasattr(self, "_user"):
            return
        for authenticator in self.authenticators:
            try:
                aauth = getattr(authenticator, "aauthenticate", None)
                if iscoroutinefunction(aauth):
                    user_auth_tuple = await aauth(self)
                elif iscoroutinefunction(authenticator.authenticate):
                    user_auth_tuple = await authenticator.authenticate(self)
                else:
                    try:
                        user_auth_tuple = authenticator.authenticate(self)
                    except SynchronousOnlyOperation:
                        user_auth_tuple = await sync_to_async(
                            authenticator.authenticate
                        )(self)
            except exceptions.APIException:
                self._not_authenticated()
                raise
            if user_auth_tuple is not None:
                self._authenticator = authenticator
                self.user, self.auth = user_auth_tuple
                await awarm_permissions(self.user)
                return
        self._not_authenticated()


# ------------------------------------------------------------------ views ----


class AsyncAPIView(adrf_views.APIView):
    def initialize_request(self, request, *args, **kwargs):
        parser_context = self.get_parser_context(request)
        return AsyncRequest(
            request,
            parsers=self.get_parsers(),
            authenticators=self.get_authenticators(),
            negotiator=self.get_content_negotiator(),
            parser_context=parser_context,
        )

    async def async_dispatch(self, request, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        request = self.initialize_request(request, *args, **kwargs)
        self.request = request
        self.headers = self.default_response_headers
        try:
            await self.ainitial(request, *args, **kwargs)
            if request.method.lower() in self.http_method_names:
                handler = getattr(
                    self, request.method.lower(), self.http_method_not_allowed
                )
            else:
                handler = self.http_method_not_allowed
            if iscoroutinefunction(handler):
                response = await handler(request, *args, **kwargs)
            else:
                response = await sync_to_async(handler)(request, *args, **kwargs)
        except Exception as exc:
            response = self.handle_exception(exc)
        self.response = self.finalize_response(request, response, *args, **kwargs)
        return self.response

    async def ainitial(self, request, *args, **kwargs):
        self.format_kwarg = self.get_format_suffix(**kwargs)
        neg = self.perform_content_negotiation(request)
        request.accepted_renderer, request.accepted_media_type = neg
        version, scheme = self.determine_version(request, *args, **kwargs)
        request.version, request.versioning_scheme = version, scheme
        await self.aperform_authentication(request)
        await self.acheck_permissions(request)
        await self.acheck_throttles(request)

    async def aperform_authentication(self, request):
        await request.aauthenticate()

    async def acheck_permissions(self, request):
        for permission in self.get_permissions():
            allowed = permission.has_permission(request, self)
            if iscoroutinefunction(permission.has_permission):
                allowed = await allowed
            if not allowed:
                self.permission_denied(
                    request,
                    message=getattr(permission, "message", None),
                    code=getattr(permission, "code", None),
                )

    async def acheck_object_permissions(self, request, obj):
        for permission in self.get_permissions():
            allowed = permission.has_object_permission(request, self, obj)
            if iscoroutinefunction(permission.has_object_permission):
                allowed = await allowed
            if not allowed:
                self.permission_denied(
                    request,
                    message=getattr(permission, "message", None),
                    code=getattr(permission, "code", None),
                )

    async def acheck_throttles(self, request):
        durations = []
        for throttle in self.get_throttles():
            allowed = throttle.allow_request(request, self)
            if iscoroutinefunction(throttle.allow_request):
                allowed = await allowed
            if not allowed:
                durations.append(throttle.wait())
        durations = [d for d in durations if d is not None]
        if durations:
            self.throttled(request, max(durations))


class AsyncGenericAPIView(AsyncAPIView, adrf_generics.GenericAPIView):
    def get_queryset(self):
        queryset = super().get_queryset()
        if isinstance(queryset, AsyncQuerySet):
            # DRF re-evaluates a sync class attribute with .all(); do the
            # same for an async one so requests never share a result cache.
            queryset = queryset.all()
        return to_async(queryset)

    async def aget_object(self):
        queryset = await self.afilter_queryset(self.get_queryset())
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        assert lookup_url_kwarg in self.kwargs, (
            "Expected view %s to be called with a URL keyword argument "
            'named "%s". Fix your URL conf, or set the `.lookup_field` '
            "attribute on the view correctly."
            % (self.__class__.__name__, lookup_url_kwarg)
        )
        filter_kwargs = {self.lookup_field: self.kwargs[lookup_url_kwarg]}
        try:
            obj = await to_async(queryset).aget(**filter_kwargs)
        except (ObjectDoesNotExist, TypeError, ValueError, DjangoValidationError):
            raise Http404
        await self.acheck_object_permissions(self.request, obj)
        return obj

    async def afilter_queryset(self, queryset):
        for backend in list(self.filter_backends):
            instance = backend()
            afilter = getattr(instance, "afilter_queryset", None)
            if iscoroutinefunction(afilter):
                queryset = await afilter(self.request, queryset, self)
            else:
                queryset = instance.filter_queryset(self.request, queryset, self)
        return queryset

    async def apaginate_queryset(self, queryset):
        if self.paginator is None:
            return None
        apaginate = getattr(self.paginator, "apaginate_queryset", None)
        if not iscoroutinefunction(apaginate):
            raise TypeError(
                "%s has no apaginate_queryset(); use AsyncPageNumberPagination "
                "or AsyncLimitOffsetPagination." % type(self.paginator).__name__
            )
        return await apaginate(queryset, self.request, view=self)

    async def get_apaginated_response(self, data):
        return self.paginator.get_paginated_response(data)


# ------------------------------------------------------------- pagination ----


class AsyncPageNumberPagination(pagination.PageNumberPagination):
    async def apaginate_queryset(self, queryset, request, view=None):
        self.request = request
        page_size = self.get_page_size(request)
        if not page_size:
            return None
        queryset = to_async(queryset)
        count = await queryset.acount()
        paginator = self.django_paginator_class(queryset, page_size)
        paginator.__dict__["count"] = count
        page_number = self.get_page_number(request, paginator)
        try:
            self.page = paginator.page(page_number)
        except InvalidPage as exc:
            msg = self.invalid_page_message.format(
                page_number=page_number, message=str(exc)
            )
            raise exceptions.NotFound(msg)
        rows = [obj async for obj in self.page.object_list]
        self.page.object_list = rows
        if paginator.num_pages > 1 and self.template is not None:
            self.display_page_controls = True
        return rows


class AsyncLimitOffsetPagination(pagination.LimitOffsetPagination):
    async def apaginate_queryset(self, queryset, request, view=None):
        self.request = request
        self.limit = self.get_limit(request)
        if self.limit is None:
            return None
        queryset = to_async(queryset)
        self.count = await queryset.acount()
        self.offset = self.get_offset(request)
        if self.count > self.limit and self.template is not None:
            self.display_page_controls = True
        if self.count == 0 or self.offset > self.count:
            return []
        return [obj async for obj in queryset[self.offset : self.offset + self.limit]]


# ----------------------------------------------------------------- mixins ----


class AsyncCreateModelMixin(adrf_mixins.CreateModelMixin):
    async def acreate(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        await ais_valid(serializer, raise_exception=True)
        await self.perform_acreate(serializer)
        data = await adrf_mixins.get_data(serializer)
        headers = self.get_success_headers(data)
        return Response(data, status=status.HTTP_201_CREATED, headers=headers)


class AsyncListModelMixin(adrf_mixins.ListModelMixin):
    async def alist(self, request, *args, **kwargs):
        queryset = await self.afilter_queryset(self.get_queryset())
        page = await self.apaginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            data = await adrf_mixins.get_data(serializer)
            return await self.get_apaginated_response(data)
        serializer = self.get_serializer(queryset, many=True)
        data = await adrf_mixins.get_data(serializer)
        return Response(data, status=status.HTTP_200_OK)


class AsyncRetrieveModelMixin(adrf_mixins.RetrieveModelMixin):
    pass


class AsyncUpdateModelMixin(adrf_mixins.UpdateModelMixin):
    async def aupdate(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = await self.aget_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        await ais_valid(serializer, raise_exception=True)
        await self.perform_aupdate(serializer)
        if getattr(instance, "_prefetched_objects_cache", None):
            instance._prefetched_objects_cache = {}
        data = await adrf_mixins.get_data(serializer)
        return Response(data, status=status.HTTP_200_OK)

    async def partial_aupdate(self, request, *args, **kwargs):
        kwargs["partial"] = True
        return await self.aupdate(request, *args, **kwargs)


class AsyncDestroyModelMixin(adrf_mixins.DestroyModelMixin):
    async def perform_adestroy(self, instance):
        await instance.async_delete()


# ---------------------------------------------------------------- generics ----


class AsyncCreateAPIView(AsyncCreateModelMixin, AsyncGenericAPIView):
    async def post(self, request, *args, **kwargs):
        return await self.acreate(request, *args, **kwargs)


class AsyncListAPIView(AsyncListModelMixin, AsyncGenericAPIView):
    async def get(self, request, *args, **kwargs):
        return await self.alist(request, *args, **kwargs)


class AsyncRetrieveAPIView(AsyncRetrieveModelMixin, AsyncGenericAPIView):
    async def get(self, request, *args, **kwargs):
        return await self.aretrieve(request, *args, **kwargs)


class AsyncDestroyAPIView(AsyncDestroyModelMixin, AsyncGenericAPIView):
    async def delete(self, request, *args, **kwargs):
        return await self.adestroy(request, *args, **kwargs)


class AsyncUpdateAPIView(AsyncUpdateModelMixin, AsyncGenericAPIView):
    async def put(self, request, *args, **kwargs):
        return await self.aupdate(request, *args, **kwargs)

    async def patch(self, request, *args, **kwargs):
        return await self.partial_aupdate(request, *args, **kwargs)


class AsyncListCreateAPIView(
    AsyncListModelMixin, AsyncCreateModelMixin, AsyncGenericAPIView
):
    async def get(self, request, *args, **kwargs):
        return await self.alist(request, *args, **kwargs)

    async def post(self, request, *args, **kwargs):
        return await self.acreate(request, *args, **kwargs)


class AsyncRetrieveUpdateAPIView(
    AsyncRetrieveModelMixin, AsyncUpdateModelMixin, AsyncGenericAPIView
):
    async def get(self, request, *args, **kwargs):
        return await self.aretrieve(request, *args, **kwargs)

    async def put(self, request, *args, **kwargs):
        return await self.aupdate(request, *args, **kwargs)

    async def patch(self, request, *args, **kwargs):
        return await self.partial_aupdate(request, *args, **kwargs)


class AsyncRetrieveDestroyAPIView(
    AsyncRetrieveModelMixin, AsyncDestroyModelMixin, AsyncGenericAPIView
):
    async def get(self, request, *args, **kwargs):
        return await self.aretrieve(request, *args, **kwargs)

    async def delete(self, request, *args, **kwargs):
        return await self.adestroy(request, *args, **kwargs)


class AsyncRetrieveUpdateDestroyAPIView(
    AsyncRetrieveModelMixin,
    AsyncUpdateModelMixin,
    AsyncDestroyModelMixin,
    AsyncGenericAPIView,
):
    async def get(self, request, *args, **kwargs):
        return await self.aretrieve(request, *args, **kwargs)

    async def put(self, request, *args, **kwargs):
        return await self.aupdate(request, *args, **kwargs)

    async def patch(self, request, *args, **kwargs):
        return await self.partial_aupdate(request, *args, **kwargs)

    async def delete(self, request, *args, **kwargs):
        return await self.adestroy(request, *args, **kwargs)


# -------------------------------------------------------------- throttles ----


class AsyncSimpleRateThrottle(throttling.SimpleRateThrottle):
    """``SimpleRateThrottle`` awaiting the cache (async with
    ``django_async_backend.core.cache.AsyncDatabaseCache``)."""

    async def allow_request(self, request, view):
        if self.rate is None:
            return True
        self.key = self.get_cache_key(request, view)
        if self.key is None:
            return True
        self.history = await self.cache.aget(self.key, [])
        self.now = self.timer()
        while self.history and self.history[-1] <= self.now - self.duration:
            self.history.pop()
        if len(self.history) >= self.num_requests:
            return self.throttle_failure()
        return await self.athrottle_success()

    async def athrottle_success(self):
        self.history.insert(0, self.now)
        await self.cache.aset(self.key, self.history, self.duration)
        return True


class AsyncAnonRateThrottle(AsyncSimpleRateThrottle, throttling.AnonRateThrottle):
    pass


class AsyncUserRateThrottle(AsyncSimpleRateThrottle, throttling.UserRateThrottle):
    pass


class AsyncScopedRateThrottle(AsyncSimpleRateThrottle, throttling.ScopedRateThrottle):
    async def allow_request(self, request, view):
        self.scope = getattr(view, self.scope_attr, None)
        if not self.scope:
            return True
        self.rate = self.get_rate()
        self.num_requests, self.duration = self.parse_rate(self.rate)
        return await AsyncSimpleRateThrottle.allow_request(self, request, view)


# --------------------------------------------------------------- viewsets ----


class AsyncGenericViewSet(adrf_viewsets.ViewSetMixin, AsyncGenericAPIView):
    # Actions are bound per request, so Django's handler-based detection
    # cannot see them; adrf's router and dispatch read this attribute.
    view_is_async = True


class AsyncReadOnlyModelViewSet(
    AsyncRetrieveModelMixin, AsyncListModelMixin, AsyncGenericViewSet
):
    pass


class AsyncModelViewSet(
    AsyncCreateModelMixin,
    AsyncListModelMixin,
    AsyncRetrieveModelMixin,
    AsyncUpdateModelMixin,
    AsyncDestroyModelMixin,
    AsyncGenericViewSet,
):
    pass
