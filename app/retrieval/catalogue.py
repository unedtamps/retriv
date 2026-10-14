"""Rendering and attribution helpers for CSV evidence.

Split out of the agent harness so the MCP tools can share them: they describe
what the row query produced, which is a fact about the data and not about any
one caller.
"""

import re
from typing import Any

from app.retrieval.sql import Catalogue


def format_catalogue(
    catalogue: Catalogue,
    *,
    header: str = "Queryable CSV files:",
    queryable: bool = True,
) -> str:
    """Render the catalogue with the document_id each query has to name.

    The id is shown because without it the model has a column list it cannot
    act on. Repetition every call is deliberate: a model already ignores
    tool_choice and retries near-identical queries, so relying on it
    remembering a list from the first call is not safe. It costs a few
    hundred tokens for an ordinary workspace and is bounded at 100 files.
    """
    if not catalogue.entries:
        return f"{header} none yet.\n\n"
    lines: list[str] = []
    if not queryable:
        # The most misleading thing a caller could do here is report missing
        # data when the data is in the workspace and the tool to read it is
        # simply switched off. That reads as a fact about the data.
        lines.append(
            "These files exist and hold rows, but this deployment cannot query "
            "them: the row-query tool is not enabled. Report the data as "
            "present but not queryable, never as absent."
        )
        lines.append("")
    lines.extend(
        f"  {entry.filename}  [{entry.document_id}]  {entry.row_count} rows"
        f"  columns: {', '.join(entry.columns) if entry.columns else 'unknown'}"
        for entry in catalogue.entries
    )
    if catalogue.hidden:
        lines.append(
            f"  ... and {catalogue.hidden} more file(s). Call list_queryable_files() to see them."
        )
    return f"{header}\n" + "\n".join(lines) + "\n\n"


def mentions_document_id(sql: str) -> bool:
    """Whether the statement names a document column anywhere.

    A presence test, not a parse. It cannot tell a filter that narrows from
    one that merely appears, which is why the result is checked again for
    spanning more than one file; between the two, a query that reaches
    another file's rows is refused rather than answered.
    """
    return bool(re.search(r"\bdocument_id\b", sql, re.IGNORECASE))


def format_values(values: list[dict[str, Any]], *, limit: int = 50) -> str:
    """Render computed rows as text a model can read.

    Aggregates are the reason the row tool exists, so their values have to
    arrive. They are capped because a group-by can return as many rows as the
    source had, and none of that needs to reach the context to answer a
    question about it.
    """
    if not values:
        return ""
    shown = values[:limit]
    lines = ["Result (computed, not citable):"]
    columns = list(shown[0])
    lines.append("  " + " | ".join(columns))
    for value in shown:
        lines.append("  " + " | ".join(str(value.get(name, "")) for name in columns))
    if len(values) > len(shown):
        lines.append(f"  ... and {len(values) - len(shown)} more row(s).")
    return "\n".join(lines) + "\n\n"
