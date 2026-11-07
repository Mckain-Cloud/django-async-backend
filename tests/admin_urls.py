"""URLconf for the async admin tests."""

from django.contrib.admin import (
    ShowFacets,
    register,
)
from django.urls import include, path
from test_app.models import (
    DatesModel,
    GenericChildModel,
    GenericRelationModel,
    M2MOwnerModel,
    M2MTagModel,
    SelectRelatedAuthorModel,
    SelectRelatedBookModel,
    SelectRelatedPublisherModel,
    TestModel,
)

from django_async_backend.contrib.admin import (
    AsyncAdminSite,
    AsyncGenericTabularInline,
    AsyncModelAdmin,
    AsyncSimpleListFilter,
    AsyncTabularInline,
)

site = AsyncAdminSite(name="admin")


class NameInitialFilter(AsyncSimpleListFilter):
    title = "initial"
    parameter_name = "initial"

    async def alookups(self, request, model_admin):
        names = [
            n
            async for n in DatesModel.async_objects.values_list(
                "name", flat=True
            ).order_by("name")
        ]
        return sorted({(n[0], n[0].upper()) for n in names if n})

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(name__startswith=self.value())
        return queryset


@register(DatesModel, site=site)
class DatesAdmin(AsyncModelAdmin):
    list_display = ["name", "date"]
    list_filter = [NameInitialFilter, "date"]
    date_hierarchy = "date"
    show_facets = ShowFacets.ALWAYS


class GenericChildInline(AsyncGenericTabularInline):
    model = GenericChildModel
    fields = ["name"]
    extra = 1


@register(GenericRelationModel, site=site)
class GenericRelationAdmin(AsyncModelAdmin):
    inlines = [GenericChildInline]


class BookInline(AsyncTabularInline):
    model = SelectRelatedBookModel
    fields = ["title", "publisher"]
    extra = 1


@register(SelectRelatedAuthorModel, site=site)
class AuthorAdmin(AsyncModelAdmin):
    list_display = ["name"]
    search_fields = ["name"]
    inlines = [BookInline]


@register(SelectRelatedBookModel, site=site)
class BookAdmin(AsyncModelAdmin):
    list_display = ["title", "author", "publisher", "review_count"]

    async def review_count(self, obj):
        # An async list_display callable: awaited per row before rendering.
        return await SelectRelatedBookModel.async_objects.filter(
            author=obj.author_id
        ).acount()

    review_count.short_description = "by same author"
    list_filter = ["author", "publisher", "title"]
    search_fields = ["title"]
    readonly_fields = ["publisher"]
    list_per_page = 5


@register(SelectRelatedPublisherModel, site=site)
class PublisherAdmin(AsyncModelAdmin):
    search_fields = ["name"]


@register(M2MOwnerModel, site=site)
class OwnerAdmin(AsyncModelAdmin):
    filter_horizontal = ["tags"]


site.register(M2MTagModel)


@register(TestModel, site=site)
class TestModelAdmin(AsyncModelAdmin):
    list_display = ["name", "value", "relative"]
    list_editable = ["value"]
    raw_id_fields = ["relative"]


# ---- REST framework (django_async_backend.contrib.rest_framework) ----------

from rest_framework import permissions  # noqa: E402

from django_async_backend.contrib import rest_framework as arf  # noqa: E402


from rest_framework import serializers as drf_serializers  # noqa: E402


class BookSerializer(arf.AsyncModelSerializer):
    author_name = drf_serializers.CharField(source="author.name", read_only=True)
    shout = drf_serializers.SerializerMethodField()

    class Meta:
        model = SelectRelatedBookModel
        fields = ["id", "title", "author", "publisher", "author_name", "shout"]

    async def aget_shout(self, obj):
        await obj.afetch("author")
        return (obj.title + " by " + obj.author.name).upper()


class OwnerSerializer(arf.AsyncModelSerializer):
    tag_names = drf_serializers.StringRelatedField(
        source="tags", many=True, read_only=True
    )

    class Meta:
        model = M2MOwnerModel
        fields = ["id", "name", "tags", "tag_names"]


class BookViewSet(arf.AsyncModelViewSet):
    queryset = SelectRelatedBookModel.objects.select_related("author").order_by("pk")
    serializer_class = BookSerializer
    authentication_classes = [arf.AsyncSessionAuthentication]
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    pagination_class = arf.AsyncPageNumberPagination
    pagination_class.page_size = 5


class OwnerViewSet(arf.AsyncModelViewSet):
    queryset = M2MOwnerModel.objects.prefetch_related("tags").order_by("pk")
    serializer_class = OwnerSerializer
    authentication_classes = [arf.AsyncSessionAuthentication]
    permission_classes = [permissions.DjangoModelPermissionsOrAnonReadOnly]
    pagination_class = arf.AsyncLimitOffsetPagination
    pagination_class.default_limit = 5


router = arf.DefaultRouter()
router.register("books", BookViewSet, basename="book")
router.register("owners", OwnerViewSet, basename="owner")

urlpatterns = [path("admin/", site.urls), path("api/", include(router.urls))]
