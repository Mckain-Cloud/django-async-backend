"""URLconf for tests/test_asgi.py."""

from django.http import (
    FileResponse,
    HttpResponse,
    JsonResponse,
)
from django.urls import path

# Set by the tests to a real file on disk.
FILE = {"path": None}


async def file_view(request):
    return FileResponse(open(FILE["path"], "rb"))


async def encoded_file_view(request):
    response = FileResponse(open(FILE["path"], "rb"))
    response["Content-Encoding"] = "gzip"  # as GZipMiddleware would
    return response


async def sliced_file_view(request):
    handle = open(FILE["path"], "rb")
    handle.seek(4)  # e.g. a range response that seeked first
    return FileResponse(handle)


async def text_view(request):
    return HttpResponse("plain")


async def state_view(request):
    return JsonResponse({"value": request.state.get("value")})


urlpatterns = [
    path("file", file_view),
    path("file-encoded", encoded_file_view),
    path("file-sliced", sliced_file_view),
    path("text", text_view),
    path("state", state_view),
]
