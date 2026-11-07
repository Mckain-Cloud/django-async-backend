import asyncio
import sys

from settings import *  # noqa

DATABASES["default"]["PORT"] = 55432
DATABASES["other"]["PORT"] = 55432
CACHES["redis"]["LOCATION"] = "redis://localhost:56379/9"

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
