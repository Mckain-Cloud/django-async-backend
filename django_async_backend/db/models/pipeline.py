"""
Evaluate several querysets in one round trip with psycopg's pipeline mode.

An ORM call sends its query and reads the result before the next call can
start, so N independent queries cost N network round trips. Inside a
pipeline, psycopg sends every statement first and reads the results back in
order; the server still runs them one after another on the one connection,
so this only removes the waiting between them. It is a latency optimisation
for batches of cheap queries, not parallelism (that still needs a second
connection, see ``async_acquire()``).

``aevaluate_pipelined(querysets)`` fills each queryset's result cache. Each
query is compiled and sent with ``SQLCompiler.execute_sql(CURSOR)``; leaving
the pipeline block syncs once, then the rows are read and handed to the
queryset's own iterable through
the same compiler instance, so model construction, ``select_related``,
annotations and converters are exactly what a normal evaluation does.

Only read-only, buffered evaluation is batched. Server-side cursors
(``aiterator()``) cannot run in a pipeline. In autocommit the statements of
one batch form a single implicit transaction, which for reads only means they
share a snapshot.

Setting ``ASYNC_BACKEND_PIPELINE``: ``True`` / ``False``; the default is on
everywhere except Windows, where a pipelined batch to a Docker Desktop
Postgres stalls for ~40 ms (TCP delayed ACK), far slower than sending the
queries one by one.
"""

import sys
from collections import defaultdict

from django.conf import settings
from django.db.models.sql.constants import CURSOR

from django_async_backend.db.utils import async_connections


def pipeline_enabled():
    return getattr(settings, "ASYNC_BACKEND_PIPELINE", sys.platform != "win32")


def _batchable(queryset):
    from django_async_backend.db.models.query import QuerySet

    return isinstance(queryset, QuerySet) and queryset._result_cache is None


class _Preloaded:
    """Make ``query.get_compiler()`` return a compiler whose rows are read."""

    def __init__(self, compiler, rows):
        self.compiler = compiler
        if compiler.has_extra_select:
            rows = [row[: compiler.col_count] for row in rows]
        self.rows = rows

    async def execute_sql(
        self, result_type=None, chunked_fetch=False, chunk_size=None
    ):
        # MULTI: a list of chunks, like cursor_iter() collected into a list.
        return [self.rows] if self.rows else []

    def install(self, query):
        compiler = self.compiler
        compiler.execute_sql = self.execute_sql
        query.get_compiler = (
            lambda using=None, connection=None, elide_empty=True: (compiler)
        )

    @staticmethod
    def uninstall(query):
        query.__dict__.pop("get_compiler", None)


async def aevaluate_pipelined(querysets):
    """
    Fill the result cache of every unevaluated async queryset in
    ``querysets``, sending the queries for each database in one pipeline.

    Querysets that are not batchable (already evaluated, not an async
    QuerySet) are left alone; a batch of one is evaluated normally. Nested
    ``prefetch_related`` lookups of a queryset run after its rows are read,
    as in a normal evaluation.
    """
    pending = [qs for qs in querysets if _batchable(qs)]
    if not pending:
        return

    if len(pending) < 2 or not pipeline_enabled():
        for qs in pending:
            await qs._fetch_all()
        return

    by_db = defaultdict(list)
    for qs in pending:
        by_db[qs.db].append(qs)

    for db, group in by_db.items():
        if len(group) < 2:
            await group[0]._fetch_all()
            continue
        await _aevaluate_group(db, group)


async def _aevaluate_group(db, group):
    connection = async_connections[db]
    await connection.ensure_connection()

    sent = []  # (queryset, compiler, cursor or None)
    try:
        # Only send inside the block. Leaving it sends one Sync and reads
        # every result into its cursor: one round trip for the batch.
        # Fetching inside the block costs a second one (the first fetch
        # syncs, then the exit syncs again), which made a two-query batch
        # slower than sending the queries one by one.
        async with connection.connection.pipeline():
            for qs in group:
                compiler = qs.query.get_compiler(using=db)
                # Compiles (filling select/klass_info/annotation_col_map) and
                # sends; returns None when the query can't match anything.
                cursor = await compiler.execute_sql(CURSOR)
                sent.append((qs, compiler, cursor))
        results = []
        for qs, compiler, cursor in sent:
            if cursor is None:
                results.append((qs, None))
                continue
            results.append((qs, _Preloaded(compiler, await cursor.fetchall())))
    finally:
        for _qs, _compiler, cursor in sent:
            if cursor is not None:
                await cursor.close()

    for qs, preloaded in results:
        if preloaded is None:
            # EmptyResultSet: a normal evaluation returns [] without a query.
            await qs._fetch_all()
            continue
        preloaded.install(qs.query)
        try:
            await qs._fetch_all()
        finally:
            _Preloaded.uninstall(qs.query)
