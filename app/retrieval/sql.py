"""Read-only SQL over CSV rows, scoped by the database rather than by the query.

The agent writes SQL, so the isolation cannot live in the statement. It lives
in four places, and this module is where three of them are applied:

1. Row Level Security on ``document_rows`` reads the scope from a session
   setting this module sets. The model's SQL never carries a workspace
   predicate and never has to, so there is no version of it that omits one.
2. The connection is refused outright if its role can bypass RLS. Without
   that check a misconfigured DSN pointing at the application role would
   silently disable the whole arrangement, because that role is a superuser.
3. The transaction is read-only with a statement timeout.
4. The statement is checked to touch only allowlisted tables, which is a
   defence against reading things RLS does not cover.

Layer 4 is the weakest and the only one that inspects model output. It exists
because RLS governs ``document_rows`` and nothing else: without it a query
naming ``workspaces`` or ``pg_authid`` would be refused by grants rather than
by this code, and a role granted more than intended would be read silently.

``set_config`` is used rather than ``SET LOCAL`` because a bind parameter is
not accepted in a SET statement, and the scope value has to be an array
literal for the policy's ``uuid[]`` cast.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

if TYPE_CHECKING:  # avoids a cycle: the agent imports this module
    from app.agent.schemas import ContextSource

# The columns a returned row is built from. A row that does not carry all of
# them is dropped: an aggregate projects a count and no id, so there is
# nothing to cite and nothing to render.
_REQUIRED_COLUMNS = (
    "id",
    "workspace_id",
    "document_id",
    "processing_run_id",
    "row_number",
    "data",
)
_ALLOWED_TABLES = frozenset({"document_rows", "documents"})

# The catalogue the model reads before writing a query. `processing_runs` is
# deliberately absent: the wrapper reads it to learn a file's columns, but the
# model has no use for it and it carries lease_token and last_error.
CATALOGUE_PAGE = 100

# Read every reference after FROM or JOIN, including inside subqueries, so a
# nested read cannot slip past. A name declared as a CTE is not a table and is
# added to the allowed set before this runs.
_TABLE_REFERENCE = re.compile(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_.]*)", re.IGNORECASE)
_CTE_NAME = re.compile(r"(?:\bwith\b|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s+as\s*\(", re.IGNORECASE)
_STATEMENT_END = re.compile(r";\s*\S")


class RowQueryError(Exception):
    """The generated query could not be run safely."""


@dataclass(frozen=True)
class RowResult:
    """One row back from SQL, in the shape the citation path already uses."""

    chunk_id: UUID
    workspace_id: UUID
    document_id: UUID
    processing_run_id: UUID
    row_number: int
    data: dict[str, object]
    columns: list[str]

    def as_source(self, filename: str | None) -> "ContextSource":
        """Render this row as evidence.

        The text is built here rather than stored, because a row's cells are
        the only copy and duplicating them per row would cost 100k copies of
        a header. Column names come from the run, which is why they are on
        the result: a row alone cannot say what its keys mean.
        """
        from app.agent.schemas import ContextSource

        rendered = " | ".join(f"{column}={self.data.get(column, '')}" for column in self.columns)
        return ContextSource(
            workspace_id=self.workspace_id,
            document_id=self.document_id,
            processing_run_id=self.processing_run_id,
            chunk_id=self.chunk_id,
            ordinal=self.row_number,
            score=1.0,
            content_hash="",
            filename=filename,
            # Empty on purpose. For a document this field is the heading path
            # and useful; here it would be the CSV's column names, which the
            # rendered text already carries and which the citation list only
            # displays as a breadcrumb reading "id / status / amount".
            section_path=[],
            page_numbers=[],
            text=rendered,
        )


@dataclass(frozen=True)
class CatalogueEntry:
    """One CSV file the model is allowed to see and query."""

    document_id: UUID
    # Carried so a computed result can be attributed to the workspace and run
    # it came from. A zero UUID here would be written into persisted source
    # references as if it were real provenance.
    workspace_id: UUID
    processing_run_id: UUID
    filename: str
    row_count: int
    columns: list[str]


@dataclass(frozen=True)
class Catalogue:
    entries: list[CatalogueEntry]
    hidden: int

    @property
    def filenames(self) -> dict[UUID, str]:
        return {entry.document_id: entry.filename for entry in self.entries}


@dataclass(frozen=True)
class RowQueryResult:
    """What one statement produced, split by whether it can be cited."""

    sources: list[RowResult]
    # Aggregate or grouped rows: no row identity, so no citation, but these
    # are usually the answer itself.
    values: list[dict[str, Any]]
    columns: list[str]

    def __bool__(self) -> bool:
        return bool(self.sources or self.values)


class CatalogueReader:
    """Lists CSV files using the application's own session.

    Exists because the catalogue and the row tool have different security
    needs, and conflating them leaves the agent unable to say anything at all
    when row queries are switched off. Telling the model which files exist is
    not a privilege escalation: the document list already returns their names
    and media types under the same workspace scope. What needs the restricted
    role is *running* a query, and that stays behind ``RowQuery``.

    Without this, a deployment with CSVs but no reader DSN gave the model a
    document search and nothing else, so it concluded the workspace held no
    such data. That reads as a fact about the data rather than as a limit on
    what the agent can do, which is the more damaging of the two.
    """

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def queryable_files(self, *, workspace_ids: tuple[UUID, ...]) -> Catalogue:
        from sqlalchemy import select as sa_select

        from app.db.models import Document, ProcessingRun

        if not workspace_ids:
            return Catalogue(entries=[], hidden=0)
        async with self._session_factory() as session:
            rows = await session.execute(
                sa_select(
                    Document.id,
                    Document.workspace_id,
                    ProcessingRun.id,
                    Document.original_filename,
                    ProcessingRun.row_count,
                    ProcessingRun.csv_columns,
                )
                .join(ProcessingRun, ProcessingRun.id == Document.active_processing_run_id)
                .where(
                    Document.workspace_id.in_(workspace_ids),
                    Document.deleted_at.is_(None),
                    Document.detected_media_type == "text/csv",
                    ProcessingRun.status == "ready",
                )
                .order_by(Document.original_filename, Document.id)
            )
            found = [tuple(r) for r in rows]
        return Catalogue(
            entries=[
                CatalogueEntry(
                    document_id=row[0],
                    workspace_id=row[1],
                    processing_run_id=row[2],
                    filename=row[3],
                    row_count=int(row[4] or 0),
                    columns=list(row[5] or []),
                )
                for row in found
            ],
            hidden=0,
        )


class RowQuery:
    """Runs one statement against the rows, inside a scope the caller sets."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        statement_timeout_ms: int,
        max_result_rows: int,
    ) -> None:
        self._engine = engine
        self._statement_timeout_ms = statement_timeout_ms
        self._max_result_rows = max_result_rows

    @classmethod
    def from_url(cls, url: str, *, statement_timeout_ms: int, max_result_rows: int) -> "RowQuery":
        return cls(
            create_async_engine(url, pool_pre_ping=True),
            statement_timeout_ms=statement_timeout_ms,
            max_result_rows=max_result_rows,
        )

    async def verify_reader(self) -> None:
        """Refuse a connection whose role ignores row-level security.

        A superuser or a role with BYPASSRLS sees every row regardless of the
        scope setting, so running on one would make the policy decorative
        while appearing to be enforced. Failing at startup is better than
        discovering it from a cross-tenant count.
        """
        async with self._engine.connect() as connection:
            result = await connection.execute(
                text("select rolsuper, rolbypassrls from pg_roles where rolname = current_user")
            )
            row = result.first()
        if row is None:
            raise RowQueryError("The configured SQL role does not exist")
        if bool(row[0]) or bool(row[1]):
            raise RowQueryError(
                "The SQL role can bypass row-level security, so workspace "
                "scoping would not apply. Point sql_database_url at a "
                "dedicated read-only role."
            )

    async def run(
        self, sql: str, *, workspace_ids: tuple[UUID, ...]
    ) -> RowQueryResult:
        """Run one statement, separating citable rows from computed values.

        Two different kinds of result come back and they must not be conflated.
        A row of a CSV carries the columns that make it citable, so it becomes
        evidence. An aggregate carries a count or a total and nothing to point
        at, so it cannot be cited -- but it is very often the entire answer,
        and returning only the column names for it made the tool report "no
        rows matched" to a question whose answer it had just computed.
        """
        self._validate(sql)
        if not workspace_ids:
            # An empty scope hides every row through RLS, but saying so
            # directly beats returning an empty result the model reads as
            # "no such data".
            raise RowQueryError("No workspace scope was provided")

        scope = "{" + ",".join(str(workspace_id) for workspace_id in workspace_ids) + "}"
        # Model-authored SQL is the point of the tool, so S608 is expected
        # here. What makes it safe: a role with SELECT grants and no RLS
        # bypass, a read-only transaction, and workspace scope applied by
        # policy rather than by this text. The allowlist is the weakest layer.
        limited = (
            f"select * from ({sql.rstrip().rstrip(';')}) "  # noqa: S608
            f"as retriv_rows limit {self._max_result_rows}"
        )

        async with self._engine.connect() as connection:
            await connection.execute(text("begin read only"))
            try:
                await connection.execute(
                    text(
                        "select set_config('statement_timeout', :timeout, true), "
                        "set_config('retriv.workspace_scope', :scope, true)"
                    ),
                    {"timeout": str(self._statement_timeout_ms), "scope": scope},
                )
                result = await connection.execute(text(limited))
                rows = result.fetchall()
                keys = tuple(result.keys())
            except DBAPIError as exc:
                raise RowQueryError(_explain(exc)) from exc
            finally:
                await connection.rollback()

        return self._to_results(rows, keys)

    def _validate(self, sql: str) -> None:
        statement = sql.strip()
        if not statement:
            raise RowQueryError("The query is empty")
        # A trailing semicolon is fine; a second statement is not.
        if _STATEMENT_END.search(statement):
            raise RowQueryError("Only a single statement may be run")
        if not statement.lower().startswith(("select", "with")):
            raise RowQueryError("Only SELECT queries may be run")
        # A CTE name is referenced by FROM but is not a table. Allowing them
        # does not widen what is readable: the body of each CTE is itself
        # scanned, and grants and row-level security hold regardless.
        permitted = _ALLOWED_TABLES | {name.lower() for name in _CTE_NAME.findall(statement)}
        for table in _TABLE_REFERENCE.findall(statement):
            name = table.split(".")[-1].lower()
            if name not in permitted:
                raise RowQueryError(f"Queries may only read {_sorted_names()}")

    def _to_results(
        self, rows: Sequence[Any], keys: tuple[str, ...]
    ) -> RowQueryResult:
        sources: list[RowResult] = []
        values: list[dict[str, Any]] = []
        for row in rows:
            mapping = dict(zip(keys, row, strict=True))
            # Extra columns are ignored rather than treated as a reason to
            # drop the row. The statement is wrapped to add a LIMIT, so a
            # plain `select *` always brings the table's own columns along
            # with it, and rejecting on those would discard every row.
            if not all(column in mapping for column in _REQUIRED_COLUMNS):
                # A computed row: no identity, so uncitable, but the values
                # must still reach the model. Unconditional -- an aggregate
                # has no `data` column at all.
                values.append(dict(mapping))
                continue
            data = mapping["data"]
            if not isinstance(data, dict):
                continue
            sources.append(
                RowResult(
                    chunk_id=mapping["id"],
                    workspace_id=mapping["workspace_id"],
                    document_id=mapping["document_id"],
                    processing_run_id=mapping["processing_run_id"],
                    row_number=int(mapping["row_number"] or 0),
                    data=dict(data),
                    columns=[str(key) for key in data],
                )
            )
        return RowQueryResult(
            sources=sources,
            values=values,
            columns=[str(key) for key in keys],
        )

    async def queryable_files(
        self,
        *,
        workspace_ids: tuple[UUID, ...],
        offset: int = 0,
        limit: int = CATALOGUE_PAGE,
        pattern: str | None = None,
    ) -> Catalogue:
        """List the CSV files in scope, with the columns each one has.

        Column names differ per file and Postgres has no schema to infer them
        from, so without this the model has to guess a key. Guessing produced
        a query against a misspelled column, which returned nothing, which
        read as "no matching data" rather than "wrong key".

        The scope comes from row-level security on both tables rather than a
        predicate here: the model cannot ask for a file it may not see,
        because the reader connection never receives that row.

        The total comes back on every row as a window count rather than from
        fetching one row past the limit: a page of two out of five cannot tell
        three were hidden by looking at the three it fetched, and reporting
        one would imply the list was nearly complete.
        """
        if not workspace_ids:
            raise RowQueryError("No workspace scope was provided")
        capped = max(1, min(limit, CATALOGUE_PAGE))
        scope = "{" + ",".join(str(value) for value in workspace_ids) + "}"
        async with self._engine.connect() as connection, connection.begin():
            await connection.execute(
                text("select set_config('retriv.workspace_scope', :s, true)"),
                {"s": scope},
            )
            conditions = [
                "d.deleted_at is null",
                "d.detected_media_type = 'text/csv'",
                # Only a finished run becomes active, so a file still
                # processing or a re-ingest that failed stays invisible
                # rather than appearing as queryable and answering nothing.
                "pr.status = 'ready'",
            ]
            parameters: dict[str, object] = {}
            if pattern:
                conditions.append("d.original_filename ilike :pattern")
                parameters["pattern"] = f"%{_escape_like(pattern)}%"
            parameters["offset"] = max(0, offset)
            parameters["limit"] = capped
            result = await connection.execute(
                text(catalogue_statement(conditions)),
                parameters,
            )
            rows = result.fetchall()
        # Last column is the window count; the six before it are projected in
        # the order given to catalogue_statement.
        total = int(rows[0][6]) if rows else 0
        return Catalogue(
            entries=[
                CatalogueEntry(
                    document_id=row[0],
                    workspace_id=row[1],
                    processing_run_id=row[2],
                    filename=row[3],
                    row_count=int(row[4] or 0),
                    columns=list(row[5] or []),
                )
                for row in rows[:capped]
            ],
            hidden=max(0, total - len(rows)),
        )

    async def aclose(self) -> None:
        await self._engine.dispose()


def catalogue_statement(conditions: list[str]) -> str:
    """Assemble the catalogue query from a fixed set of literal fragments.

    Every fragment here is a constant, and the values they bind to are
    parameters, so nothing model-authored reaches this string. Kept out of
    the caller so the one place that composes SQL reads as obviously literal.
    """
    return (
        # Fragments are literals and values bind as parameters, so nothing
        # model-authored reaches this string. count(*) over () counts past
        # limit/offset, letting the catalogue report what it is not showing.
        "select d.id, d.workspace_id, pr.id, "  # noqa: S608
        "d.original_filename, pr.row_count, pr.csv_columns, "
        "count(*) over () as total "
        "from documents d "
        "join processing_runs pr on pr.id = d.active_processing_run_id "
        f"where {' and '.join(conditions)} "
        # A total order, so paging cannot repeat or skip a file when two
        # share a name.
        "order by d.original_filename, d.id "
        "limit :limit offset :offset"
    )


_UNDEFINED_COLUMN = re.compile(r'column "([^"]+)" does not exist', re.IGNORECASE)
_UNDEFINED_RELATION = re.compile(r'relation "([^"]+)" does not exist', re.IGNORECASE)


def _explain(exc: DBAPIError) -> str:
    """Turn a Postgres failure into something the model can act on.

    A bare "could not be executed" is a dead end, and a model that cannot
    learn from a failure retries until the tool budget runs out and then
    reports the data as missing. Observed on the first live run: eight
    queries written as if the CSV headers were columns, every one rejected,
    and no hint that the cells live inside `data`. Correcting the syntax
    turned the same question into one successful query.

    SQLAlchemy wraps asyncpg's error, so the specific type is only present as
    text inside the message. That also means Postgres has already lowercased
    the identifier it complains about, which matters: a CSV header may be
    `Revenue_USD` and JSONB keys are case-sensitive, so echoing the name
    straight back would hand out a key that matches nothing.
    """
    message = str(getattr(exc, "orig", exc))

    relation = _UNDEFINED_RELATION.search(message)
    if relation:
        return (
            f"There is no table named '{relation.group(1)}' that may be read. "
            "The readable tables are document_rows and documents."
        )

    column = _UNDEFINED_COLUMN.search(message)
    if column:
        raw = column.group(1)
        return (
            f"There is no column named '{raw}'. A CSV has no columns of its own: "
            "every cell lives inside the JSONB column 'data', keyed by the CSV "
            "header exactly as the catalogue spells it, including capitals. "
            f"Read a number as (data->>'Revenue_USD')::numeric and text as "
            "data->>'Header', matching a whole value with data @> "
            "'{\"Key\": \"value\"}'. Copy the key from the catalogue rather "
            "than from this message: this one has been lowercased by Postgres."
        )

    if "operator does not exist" in message or "SyntaxError" in message or (
        "syntax error" in message
    ):
        return (
            "That syntax is not valid. CSV cells are values inside a JSONB "
            "column, not columns to compare: use data->>'Name' = 'value' rather "
            "than Name = 'value', and cast before comparing numbers, e.g. "
            "(data->>'Amount')::numeric > 5000."
        )

    if "argument of" in message and "must be type" in message:
        return (
            "That cast did not match the column's type. Values are stored as "
            "text, so cast explicitly: (data->>'Amount')::numeric for numbers."
        )

    return "The query could not be executed."


def _escape_like(value: str) -> str:
    """Stop a search term from acting as a wildcard.

    The term reaches Postgres as a bind parameter, so it cannot inject SQL.
    It could still match every file through ``%`` or ``_``, which turns a
    lookup for one name into the whole catalogue.
    """
    for character in ("\\", "%", "_"):
        value = value.replace(character, "\\" + character)
    return value


def _sorted_names() -> str:
    return ", ".join(sorted(_ALLOWED_TABLES))


# Re-exported for the annotation on RowResult.as_source without importing the
# agent package at module load, which would be a cycle.
if False:  # pragma: no cover
    from app.agent.schemas import ContextSource
