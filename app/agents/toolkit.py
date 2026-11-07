"""The retrieval tools, bound to one caller, independent of any transport.

The MCP server and the in-server agent harness expose the same capabilities:
search, navigation, the row queries, downloads and pictures. This module is
where those capabilities actually live. The MCP tools are thin adapters that
authenticate a request and delegate here; the harness calls the methods
directly, with no protocol in between.

Every method takes the caller's scope as already resolved -- the toolkit is
constructed per request with a fresh principal, so revoking a credential
bites on the next call wherever the call came from.

Failures the caller could act on are raised as ``ToolRejection`` with a
message meant to be read by a model; ``AppError`` carries the ones an HTTP
client would recognise.
"""

import tempfile
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from anyio import to_thread
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Document,
    DocumentChunk,
    DocumentImage,
    DocumentWorkspace,
    ProcessingRun,
    Workspace,
    WorkspaceMembership,
)
from app.infrastructure.storage import ObjectNotFoundError, ObjectTooLargeError, StorageError
from app.mcp.auth import McpPrincipal, require_workspace
from app.mcp.documents import find_documents as search_documents
from app.mcp.documents import format_documents
from app.mcp.overview import (
    DocumentReport,
    WorkspaceSummary,
    format_document_report,
    format_workspaces,
)
from app.mcp.runtime import McpRuntime
from app.processing.service import derived_document_status
from app.retrieval.catalogue import format_catalogue, format_values, mentions_document_id
from app.retrieval.format import (
    format_context,
    format_outline,
    navigation_footer,
    navigation_hint,
    source_indices,
)
from app.retrieval.navigation import DocumentNotFound, DocumentNotNavigable
from app.retrieval.schemas import ContextSource
from app.retrieval.sql import (
    ROW_STORED_MEDIA_TYPES,
    Catalogue,
    RowQueryError,
    catalogue_entries,
)

if TYPE_CHECKING:
    from app.infrastructure.storage import ArtifactStorage


class ToolRejection(RuntimeError):
    """A failure the calling model could correct, message addressed to it."""


@dataclass(frozen=True)
class ImagePayload:
    """One extracted picture: the bytes, and the text that goes beside them."""

    data: bytes
    media_type: str
    text: str


def _by_chunk(sources: list[ContextSource]) -> dict[UUID, ContextSource]:
    return {source.chunk_id: source for source in sources}


async def _scoped_document(
    session: AsyncSession, workspace_id: UUID, document_id: UUID
) -> Document:
    """The document a workspace-scoped tool may touch, or the error to raise.

    The document must be linked into the named workspace through
    ``document_workspaces``, and neither link nor document may be under
    deletion. The failure is the same for "not yours" and "does not exist".
    """
    row = (
        await session.execute(
            select(Document, DocumentWorkspace)
            .join(
                DocumentWorkspace,
                DocumentWorkspace.document_id == Document.id,
            )
            .where(
                Document.id == document_id,
                DocumentWorkspace.workspace_id == workspace_id,
            )
        )
    ).one_or_none()
    if row is None:
        raise ToolRejection("No such document in that workspace.")
    document, document_link = row
    if document.deleted_at is not None or document_link.deleted_at is not None:
        raise ToolRejection("That document is being deleted and can no longer be read.")
    return document


def _artifact_bytes(
    storage: "ArtifactStorage", bucket: str, key: str, max_bytes: int
) -> bytes:
    """One artifact object fully in memory, bounded by the picture cap."""
    with tempfile.TemporaryDirectory(prefix="mcp-image-") as folder:
        path = Path(folder) / "artifact"
        storage.download_file(path, bucket=bucket, key=key, max_bytes=max_bytes)
        return path.read_bytes()


class AgentToolKit:
    """The read tools over one principal's scope, ready to call."""

    def __init__(self, runtime: McpRuntime, principal: McpPrincipal) -> None:
        self._runtime = runtime
        self._principal = principal

    @property
    def row_queries(self) -> bool:
        """Whether this deployment can serve row queries at all."""
        return self._runtime.row_query is not None

    @property
    def default_search_limit(self) -> int:
        """The server's default match count for a search that names none."""
        return self._runtime.settings.max_context_chunks

    async def search_context(
        self,
        *,
        query: str,
        limit: int,
        workspace_id: UUID | None = None,
        document_id: UUID | None = None,
        min_score: float | None = None,
    ) -> str:
        scope = self._scope(workspace_id)
        if not scope:
            return "No searchable workspace is in scope for this API key."
        sources = await self._runtime.retriever.retrieve(
            query,
            scope,
            limit,
            budget=self._runtime.settings.max_context_sources,
            min_score=min_score,
            document_id=document_id,
        )
        if not sources:
            return "No relevant sources were found."
        indices = source_indices(_by_chunk(sources))
        body = format_context(sources, indices)
        hint = navigation_hint(sources, indices)
        return f"{body}\n\n{hint}" if hint else body

    async def search_text(
        self,
        *,
        query: str,
        limit: int,
        workspace_id: UUID | None = None,
        document_id: UUID | None = None,
    ) -> str:
        """Literal matches, for identifiers an embedding cannot resolve."""
        scope = self._scope(workspace_id)
        if not scope:
            return "No searchable workspace is in scope for this API key."
        sources = await self._runtime.retriever.search_text(
            query, scope, limit, document_id=document_id
        )
        if not sources:
            return (
                f"No chunk contains the literal text {query!r}. This search "
                "matches exact words rather than meaning; use search_context "
                "for a paraphrase."
            )
        return format_context(sources, source_indices(_by_chunk(sources)))

    async def read_document(
        self,
        *,
        document_id: UUID,
        workspace_id: UUID,
        start_ordinal: int = 0,
        limit: int = 20,
    ) -> str:
        scope = self._scope(workspace_id)
        window = await self._runtime.navigator.read_window(
            document_id=document_id,
            workspace_ids=scope,
            start_ordinal=start_ordinal,
            limit=limit,
        )
        if not window.sources:
            return navigation_footer(window)
        indices = source_indices(_by_chunk(window.sources))
        body = format_context(window.sources, indices)
        return f"{body}\n\n{navigation_footer(window)}"

    async def get_document_outline(
        self, *, document_id: UUID, workspace_id: UUID, limit: int = 200
    ) -> str:
        """A document's sections, so a long read starts at the right place."""
        scope = self._scope(workspace_id)
        try:
            outline = await self._runtime.navigator.outline(
                document_id=document_id,
                workspace_ids=scope,
                limit=limit,
            )
        except (DocumentNotFound, DocumentNotNavigable) as exc:
            raise ToolRejection(str(exc)) from exc
        return format_outline(outline)

    async def list_queryable_files(
        self,
        *,
        offset: int = 0,
        limit: int = 100,
        pattern: str | None = None,
    ) -> str:
        runtime = self._runtime
        scope = tuple(self._principal.workspace_ids)
        if not scope:
            return format_catalogue(
                Catalogue(entries=[], hidden=0),
                header="CSV files:",
                queryable=runtime.row_query is not None,
            )
        catalogue = await self._catalogue_for(
            scope, offset=offset, limit=limit, pattern=pattern
        )
        body = format_catalogue(
            catalogue, header="CSV files:", queryable=runtime.row_query is not None
        )
        if offset:
            body = f"{body}\n(showing from offset {offset})"
        if runtime.row_query is None and (offset or pattern):
            body += (
                "\nPaging and filename search need the row-query reader, which "
                "this deployment does not have; the list above is the whole "
                "catalogue.\n"
            )
        return body

    async def find_documents(
        self,
        *,
        pattern: str | None = None,
        workspace_id: UUID | None = None,
        limit: int = 20,
    ) -> str:
        scope = self._scope(workspace_id)
        if not scope:
            return "No searchable workspace is in scope for this API key."
        async with self._runtime.session_factory() as session:
            summaries, total = await search_documents(
                session, scope, pattern=pattern, limit=limit
            )
        return format_documents(summaries, total=total, pattern=pattern)

    async def list_workspaces(self) -> str:
        """The workspaces this credential may read, with their sizes.

        The document tools return a workspace_id but never a name, so without
        this a question about "the legal workspace" can only be answered by
        guessing which id belongs to it. Membership is the scope: the same rows
        that decided ``workspace_ids`` decide this list.
        """
        scope = tuple(self._principal.workspace_ids)
        if not scope:
            return "No workspaces are in scope for this API key."
        async with self._runtime.session_factory() as session:
            rows = (
                await session.execute(
                    select(Workspace, WorkspaceMembership.role)
                    .join(
                        WorkspaceMembership,
                        WorkspaceMembership.workspace_id == Workspace.id,
                    )
                    .where(
                        Workspace.id.in_(scope),
                        WorkspaceMembership.user_id == self._principal.user.id,
                    )
                    .order_by(Workspace.slug)
                )
            ).all()
            counts: dict[UUID, int] = {}
            for workspace_id, total in (
                await session.execute(
                    select(DocumentWorkspace.workspace_id, func.count())
                    .join(Document, Document.id == DocumentWorkspace.document_id)
                    .where(
                        DocumentWorkspace.workspace_id.in_(scope),
                        DocumentWorkspace.deleted_at.is_(None),
                        Document.deleted_at.is_(None),
                    )
                    .group_by(DocumentWorkspace.workspace_id)
                )
            ).all():
                counts[workspace_id] = int(total)
        return format_workspaces(
            [
                WorkspaceSummary(
                    workspace_id=workspace.id,
                    slug=workspace.slug,
                    name=workspace.name,
                    role=role,
                    description=workspace.description,
                    documents=int(counts.get(workspace.id, 0)),
                )
                for workspace, role in rows
            ]
        )

    async def describe_document(self, *, document_id: UUID, workspace_id: UUID) -> str:
        """What the pipeline extracted, so absence is not mistaken for silence.

        ``find_documents`` reports whether a document is ready; this reports
        what ready actually contains. A scanned PDF with no text layer is
        ready and yields zero chunks, and a model that only sees "ready" will
        answer "the document does not mention it" to a question the extraction
        never gave it a chance to answer.
        """
        runtime = self._runtime
        async with runtime.session_factory() as session:
            workspace = await require_workspace(
                session,
                user=self._principal.user,
                workspace_id=workspace_id,
                permission="read",
            )
            document = await _scoped_document(session, workspace_id, document_id)
            link = await session.scalar(
                select(DocumentWorkspace).where(
                    DocumentWorkspace.document_id == document.id,
                    DocumentWorkspace.workspace_id == workspace_id,
                )
            )
            run = (
                await session.get(ProcessingRun, document.active_processing_run_id)
                if document.active_processing_run_id is not None
                else None
            )
            chunks = 0
            if run is not None:
                chunks = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(DocumentChunk)
                        .where(DocumentChunk.processing_run_id == run.id)
                    )
                    or 0
                )
            images = int(
                await session.scalar(
                    select(func.count())
                    .select_from(DocumentImage)
                    .where(DocumentImage.document_id == document.id)
                )
                or 0
            )
            described = int(
                await session.scalar(
                    select(func.count())
                    .select_from(DocumentImage)
                    .where(
                        DocumentImage.document_id == document.id,
                        DocumentImage.described.is_(True),
                    )
                )
                or 0
            )

        row_stored = document.detected_media_type in ROW_STORED_MEDIA_TYPES
        pages = 0
        sections = 0
        if not row_stored and run is not None:
            outline = await runtime.navigator.outline(
                document_id=document.id,
                workspace_ids=[workspace_id],
                limit=1,
            )
            pages = outline.page_count
            sections = outline.section_count

        columns: list[str] = []
        if run is not None and run.csv_columns:
            for _sheet, sheet_columns, _rows in catalogue_entries(
                run.csv_columns, run.row_count
            ):
                for column in sheet_columns:
                    if column not in columns:
                        columns.append(column)

        return format_document_report(
            DocumentReport(
                filename=link.filename if link is not None else document.original_filename,
                document_id=document.id,
                workspace_name=workspace.name,
                workspace_id=workspace_id,
                media_type=document.detected_media_type,
                size_bytes=document.size_bytes,
                uploaded_at=document.created_at,
                status=derived_document_status(document, run),
                pipeline=(
                    f"{run.job_kind} {run.pipeline_version}" if run is not None else None
                ),
                chunks=chunks,
                embedded_chunks=run.embedded_chunk_count if run is not None else 0,
                pages=pages,
                sections=sections,
                images=images,
                described_images=described,
                rows=run.row_count if run is not None else 0,
                columns=columns,
                last_error=run.last_error if run is not None else None,
                queryable=self.row_queries,
                row_stored=row_stored,
            )
        )

    async def query_rows(self, *, sql: str) -> str:
        runtime = self._runtime
        rows = runtime.row_query
        if rows is None:
            raise ToolRejection("Row queries are not enabled on this deployment.")
        scope = tuple(self._principal.workspace_ids)
        if not scope:
            return format_catalogue(Catalogue(entries=[], hidden=0))
        if not mentions_document_id(sql):
            prefix = await self._rejection_prefix(scope)
            raise ToolRejection(
                f"{prefix}The query was rejected: it does not name a "
                "document_id, so it could read any file at once. Add "
                "AND document_id = '<uuid from the catalogue>' and run it "
                "once per file."
            )
        try:
            outcome = await rows.run(sql, workspace_ids=scope)
        except RowQueryError as exc:
            prefix = await self._rejection_prefix(scope)
            raise ToolRejection(f"{prefix}The query was rejected: {exc}") from exc

        computed = format_values(outcome.values)
        if outcome.sources:
            filenames = await runtime.catalogue_reader.filenames(
                workspace_ids=scope,
                document_ids={result.document_id for result in outcome.sources},
            )
            sources = [
                result.as_source(filenames.get(result.document_id))
                for result in outcome.sources
            ]
            body = format_context(sources, source_indices(_by_chunk(sources)))
            return f"{body}{computed}"
        if computed:
            return (
                f"{computed}\n"
                "These values are computed from the rows, so there is no "
                "single row to cite. Give the number and name the file it "
                "came from.\n"
            )
        return (
            "No rows matched. Check the document_id and column names "
            "against the catalogue from list_queryable_files; a "
            "misspelled column returns nothing."
        )

    async def download_document(self, *, workspace_id: UUID, document_id: UUID) -> str:
        runtime = self._runtime
        async with runtime.session_factory() as session:
            await require_workspace(
                session,
                user=self._principal.user,
                workspace_id=workspace_id,
                permission="download",
            )
            row = (
                await session.execute(
                    select(Document, DocumentWorkspace)
                    .join(
                        DocumentWorkspace,
                        DocumentWorkspace.document_id == Document.id,
                    )
                    .where(
                        Document.id == document_id,
                        DocumentWorkspace.workspace_id == workspace_id,
                    )
                )
            ).one_or_none()
            if row is None:
                raise ToolRejection("No such document in that workspace.")
            document, document_link = row
            if document.deleted_at is not None or document_link.deleted_at is not None:
                raise ToolRejection(
                    "That document is being deleted and can no longer be downloaded."
                )

        ttl = runtime.settings.mcp_download_link_ttl_seconds
        try:
            link = await to_thread.run_sync(
                partial(
                    runtime.storage.presign_get,
                    bucket=document.bucket,
                    key=document.object_key,
                    expires_in=ttl,
                )
            )
        except StorageError as exc:
            raise ToolRejection(
                "The document could not be signed for download. Its url still "
                "works: "
                f"/v1/workspaces/{workspace_id}/documents/{document_id}/download"
            ) from exc

        return "\n".join(
            (
                f"link={link}",
                f"link_expires_in_seconds={ttl}",
                f"url=/v1/workspaces/{workspace_id}/documents/{document_id}/download",
                f"filename={document_link.filename}",
                f"media_type={document.detected_media_type}",
                f"size_bytes={document.size_bytes}",
            )
        )

    async def list_images(self, *, workspace_id: UUID, document_id: UUID) -> str:
        async with self._runtime.session_factory() as session:
            await require_workspace(
                session,
                user=self._principal.user,
                workspace_id=workspace_id,
                permission="read",
            )
            document = await _scoped_document(session, workspace_id, document_id)
            images = (
                (
                    await session.execute(
                        select(DocumentImage)
                        .where(DocumentImage.document_id == document.id)
                        .order_by(DocumentImage.page_number, DocumentImage.ref)
                    )
                )
                .scalars()
                .all()
            )
        if not images:
            return "This document has no extracted pictures."
        lines = [f"{len(images)} picture(s):"]
        for image in images:
            state = "described" if image.described else "undescribed"
            line = (
                f"{image.ref} page={image.page_number} {image.media_type} "
                f"{image.size_bytes}B {state}"
            )
            if image.caption:
                line += f" caption={image.caption}"
            lines.append(line)
        return "\n".join(lines)

    async def get_image(
        self, *, workspace_id: UUID, document_id: UUID, ref: str
    ) -> ImagePayload:
        runtime = self._runtime
        async with runtime.session_factory() as session:
            await require_workspace(
                session,
                user=self._principal.user,
                workspace_id=workspace_id,
                permission="read",
            )
            document = await _scoped_document(session, workspace_id, document_id)
            image = await session.scalar(
                select(DocumentImage).where(
                    DocumentImage.document_id == document.id,
                    DocumentImage.ref == ref,
                )
            )
            if image is None:
                raise ToolRejection(
                    "No such picture in that document; list_images names the ones it has."
                )

        key = image.object_key
        try:
            data = await to_thread.run_sync(
                partial(
                    _artifact_bytes,
                    runtime.artifact_storage,
                    image.bucket,
                    key,
                    runtime.settings.max_image_bytes,
                )
            )
        except ObjectNotFoundError as exc:
            raise ToolRejection(
                "That picture's bytes are missing from storage; the document may "
                "need re-ingesting."
            ) from exc
        except ObjectTooLargeError as exc:
            raise ToolRejection(
                "That picture exceeds the serving size limit and cannot be sent."
            ) from exc
        except StorageError as exc:
            raise ToolRejection("The picture could not be read from storage.") from exc

        media_type = image.media_type
        parts = [
            f"ref={image.ref}",
            f"page={image.page_number}",
            f"media_type={media_type}",
            f"size_bytes={len(data)}",
            f"described={str(image.described).lower()}",
        ]
        if image.caption:
            parts.append(f"caption={image.caption}")
        if image.description:
            parts.append(f"description={image.description}")
        return ImagePayload(data=data, media_type=media_type, text="\n".join(parts))

    def _scope(self, workspace_id: UUID | None) -> list[UUID]:
        """The workspaces one read may span.

        Without an argument it is everything the credential can see; with one
        it is that workspace alone, and only if the credential already saw
        it. Naming a workspace cannot grant access to it, so the failure
        message is the same for "not yours" and "does not exist".
        """
        if workspace_id is None:
            return self._principal.workspace_ids
        if workspace_id not in self._principal.workspace_ids:
            raise ToolRejection("That workspace is not in scope for this API key.")
        return [workspace_id]

    async def _catalogue_for(
        self,
        workspace_ids: tuple[UUID, ...],
        *,
        offset: int = 0,
        limit: int = 100,
        pattern: str | None = None,
    ) -> Catalogue:
        """The queryable CSV files, without caching across tool calls.

        The agent cached these per answer because a turn made many calls; each
        MCP call stands alone, so the honest list is the current one.
        """
        runtime = self._runtime
        if not workspace_ids:
            return Catalogue(entries=[], hidden=0)
        if runtime.row_query is None:
            return await runtime.catalogue_reader.queryable_files(
                workspace_ids=workspace_ids
            )
        return await runtime.row_query.queryable_files(
            workspace_ids=workspace_ids, offset=offset, limit=limit, pattern=pattern
        )

    async def _rejection_prefix(self, scope: tuple[UUID, ...]) -> str:
        """The catalogue, fetched only when a rejection needs to hand it back.

        Successful queries used to carry it too, which repeated up to 100 files
        with their columns in every answer; the model fetches it once with
        list_queryable_files instead.
        """
        catalogue = await self._catalogue_for(scope)
        return format_catalogue(catalogue, queryable=True)
