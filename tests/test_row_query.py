"""Statement validation for the row-query tool.

These run without a database on purpose. What they pin is the shape check that
happens before anything is sent: the table allowlist and the single-statement
rule. The isolation itself is row-level security, and that can only be shown
against a real Postgres -- see ``test_postgres_integration.py``.

The allowlist is the weakest layer of the four and is not what isolates
anything. It is here because RLS governs ``document_rows`` alone: without a
check, a query naming ``workspaces`` would be refused by grants rather than by
this code, and a role granted more than intended would be read without
comment.
"""


from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.retrieval.sql import RowQuery, RowQueryError, _explain


def runner(**overrides: int) -> RowQuery:
    # The engine is never connected to here; validation runs before any I/O.
    settings = {
        "statement_timeout_ms": 5_000,
        "max_result_rows": 200,
    } | overrides
    return RowQuery(
        create_async_engine("postgresql+asyncpg://user:pw@localhost/none"),
        **settings,
    )


@pytest.mark.parametrize(
    "sql",
    [
        "select * from document_rows",
        "select count(*) from document_rows",
        "select data->>'status' as status, count(*) from document_rows group by 1",
        "select * from document_rows join documents on documents.id = document_rows.document_id",
        "select * from document_rows where data->>'amount' > '5000' order by row_number",
        "SELECT * FROM DOCUMENT_ROWS",
        "select * from document_rows;",  # one trailing semicolon is fine
        "  select * from document_rows  ",
    ],
)
def test_readable_queries_are_accepted(sql: str) -> None:
    runner()._validate(sql)


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("", "empty"),
        ("   ", "blank"),
        ("select * from workspaces", "table not allowlisted"),
        ("select * from pg_authid", "catalog not allowlisted"),
        ("select * from document_rows; drop table document_rows", "two statements"),
        ("select * from document_rows; select 1", "two statements"),
        ("delete from document_rows", "not a select"),
        ("update document_rows set row_number = 1", "not a select"),
        ("insert into document_rows values (1)", "not a select"),
        ("drop table document_rows", "not a select"),
        ("copy document_rows to '/tmp/out'", "not a select"),
        ("select * from workspaces join document_rows on true", "join target"),
    ],
)
def test_rejected_queries_are_refused(sql: str, reason: str) -> None:
    with pytest.raises(RowQueryError):
        runner()._validate(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "with a as (select * from document_rows), b as (select * from a) "
        "select count(*) from b",
        "with w as (select 1 as n) select * from w",
    ],
)
def test_ctes_are_allowed_but_their_bodies_are_still_scanned(sql: str) -> None:
    """A CTE name is not a table, so FROM on it must not be refused.

    Its body is scanned like any other reference, so allowing the name does
    not widen what is readable.
    """
    runner()._validate(sql)


def test_a_cte_cannot_smuggle_a_disallowed_table() -> None:
    with pytest.raises(RowQueryError):
        runner()._validate(
            "with sneaky as (select * from workspaces) select * from sneaky"
        )


def test_a_subquery_cannot_escape_the_allowlist() -> None:
    """The reference scan covers FROM inside a subquery too.

    A regex is a weak check and this is where its weakness would show: the
    pattern is not a parser, so it is written to be conservative about what it
    misses and backed by grants and RLS rather than trusted alone.
    """
    with pytest.raises(RowQueryError):
        runner()._validate("select * from (select * from workspaces) as w")


def test_a_schema_qualified_allowlisted_name_is_accepted() -> None:
    runner()._validate("select * from public.document_rows")


def test_the_error_names_only_allowlisted_tables() -> None:
    """Rejections are read back by the model.

    Echoing the rejected statement would leak schema detail, so the message
    lists what is permitted instead of what was asked for.
    """
    with pytest.raises(RowQueryError) as caught:
        runner()._validate("select * from pg_authid")
    message = str(caught.value)
    assert "document_rows" in message
    assert "pg_authid" not in message


@pytest.mark.asyncio
async def test_an_empty_scope_is_refused_rather_than_returning_nothing() -> None:
    """RLS would hide every row anyway; saying so is clearer.

    An empty result is read by the model as "no such data", which is a
    different claim from "not permitted to look", and the second is the truth.
    """
    with pytest.raises(RowQueryError):
        await runner().run("select * from document_rows", workspace_ids=())

def _failing(error: Exception) -> DBAPIError:
    return DBAPIError("select 1", {}, error)


def test_a_missing_column_is_explained_in_terms_the_model_can_use() -> None:
    """The hint is what turned eight wrong queries into one correct one.

    On the first live run the model wrote the CSV headers as if they were SQL
    columns. Every statement failed with `column "revenue_usd" does not
    exist`, the tool reported "could not be executed", and the model burned
    its whole budget retrying before reporting the data as missing. Given the
    JSONB hint it corrected the syntax on the next attempt and answered.
    """
    error = _failing(
        RuntimeError(
            '<class \'asyncpg.exceptions.UndefinedColumnError\'>: '
            'column "revenue_usd" does not exist'
        )
    )
    message = _explain(error)
    assert "revenue_usd" in message
    assert "data->>" in message
    # Postgres lowercased the identifier it complained about, and JSONB keys
    # are case-sensitive, so echoing it back must not read as the real key.
    assert "lower" in message.lower()


def test_a_missing_relation_names_the_readable_tables() -> None:
    error = _failing(
        RuntimeError(
            '<class \'asyncpg.exceptions.UndefinedTableError\'>: '
            'relation "pg_authid" does not exist'
        )
    )
    message = _explain(error)
    # The name is echoed because the model wrote it itself, so it reveals
    # nothing, and it tells the model which table it actually reached for.
    assert "pg_authid" in message
    assert "document_rows" in message
    assert "documents" in message


def test_an_unknown_failure_stays_generic() -> None:
    """A guess about the cause is worse than no guess."""
    assert (
        _explain(_failing(RuntimeError("connection reset by peer")))
        == "The query could not be executed."
    )


_ROW_KEYS = (
    "id",
    "workspace_id",
    "document_id",
    "processing_run_id",
    "row_number",
    "data",
)


def _result_row(document_id: UUID) -> tuple[object, ...]:
    """One row, shaped the way Postgres hands it back."""
    return (
        uuid4(),
        uuid4(),
        document_id,
        uuid4(),
        1,
        {"id": "1", "amount": "1500.00"},
    )


def test_a_result_spanning_two_files_is_refused() -> None:
    """The statement's own text is not the witness; the rows are.

    A ``document_id`` can appear in a statement without narrowing anything,
    so no presence test on the SQL can tell two files apart. The rows are the
    only thing that knows. This is the single place the rule is enforced now:
    ``RowQuery.run`` applies it, and neither the agent nor the MCP tool
    carries its own copy.
    """
    with pytest.raises(RowQueryError, match="rows came from 2 files"):
        runner()._to_results([_result_row(uuid4()), _result_row(uuid4())], _ROW_KEYS)


def test_a_result_from_one_file_is_kept() -> None:
    document_id = uuid4()
    outcome = runner()._to_results(
        [_result_row(document_id), _result_row(document_id)], _ROW_KEYS
    )
    assert [row.document_id for row in outcome.sources] == [document_id, document_id]


def test_a_value_only_result_has_nothing_to_span() -> None:
    """An aggregate returns no rows, so there is nothing for the check to see.

    It is still guarded, by the caller's requirement that the statement name
    a document_id -- which is why that check stays where it is rather than
    moving in here.
    """
    outcome = runner()._to_results([(12,)], ("count",))
    assert outcome.sources == []
    assert outcome.values == [{"count": 12}]
