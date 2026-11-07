from django_async_backend.db.new_connection import (
    async_acquire,
    async_new_connection,
)
from django_async_backend.db.utils import async_connections

__all__ = [
    "async_acquire",
    "async_connections",
    "async_new_connection",
    "close_old_async_connections",
]


def _in_testcase_transaction(conn):
    """True while ``conn`` is inside a transaction opened by a test case
    (AsyncioTestCase, the pytest ``adb`` fixture). Like Django's test client,
    which disconnects ``close_old_connections`` around a request, we must
    not close such a connection: the test's data lives in it.
    """
    blocks = getattr(conn, "atomic_blocks", None)
    return bool(
        conn.in_atomic_block is True
        and blocks
        and getattr(blocks[0], "_from_testcase", False) is True
    )


async def close_old_async_connections(**kwargs):
    # Close every configured connection alias. close() is a no-op on a
    # wrapper with no underlying connection, so walking aliases this
    # task hasn't touched is free. **kwargs lets the same function be
    # connected as a signal handler.
    for conn in async_connections.all():
        if _in_testcase_transaction(conn):
            continue
        await conn.close()
