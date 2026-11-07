from django import template
from test_app.models import SelectRelatedBookModel

register = template.Library()


@register.simple_tag
async def book_count(author):
    return await SelectRelatedBookModel.async_objects.filter(
        author=author
    ).acount()


@register.simple_tag
def shout(value):
    return str(value).upper()


@register.simple_tag(takes_context=True)
async def author_titles(context, author):
    titles = [
        t
        async for t in author.books.async_objects.order_by(
            "title"
        ).values_list("title", flat=True)
    ]
    context["titles"] = titles
    return ""


@register.inclusion_tag("inc_books.html")
async def books_of(author):
    books = [b async for b in author.books.async_objects.order_by("title")]
    return {"books": books}


@register.filter
def double(value):
    return value * 2


@register.simple_tag
def sync_book_count(author):
    """A third-party-style tag: queries the sync ORM in render()."""
    return SelectRelatedBookModel.objects.filter(author=author).count()
