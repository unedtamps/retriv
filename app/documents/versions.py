"""Workspace-scoped document versioning and candidate detection.

Canonical document intelligence lives on ``DocumentProfile`` (global per
``document_id``), whereas version relationships are strictly workspace-scoped
because two tenants may share a deduplicated canonical document while managing
entirely different document sets.

Auto-detected version matches are persisted as ``pending`` suggestions that
require manual confirmation by a workspace ``owner`` or ``editor``. Once
confirmed (or manually linked), members share a ``DocumentVersionGroup`` with
explicit ``version_rank`` ordering and ``is_latest`` flags.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from uuid import UUID

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.models import (
    VERSION_RELATION_KINDS,
    VERSION_RELATION_PERIODIC_SERIES,
    VERSION_RELATION_SUPERSEDES,
    VERSION_SUGGESTION_CONFIRMED,
    VERSION_SUGGESTION_DISMISSED,
    VERSION_SUGGESTION_PENDING,
    Document,
    DocumentProfile,
    DocumentVersionGroup,
    DocumentVersionMember,
    DocumentVersionSuggestion,
    DocumentWorkspace,
)
from app.documents.intelligence import normalize_entity_name

logger = logging.getLogger(__name__)

_PERIODIC_CATEGORIES = frozenset({"financial_report", "meeting_memo", "invoice_receipt"})
_SUGGESTION_THRESHOLD = 0.55
_MAX_SUGGESTIONS_PER_DOC = 3

_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "this",
        "that",
        "of",
        "in",
        "to",
        "on",
        "by",
        "as",
        "at",
        "an",
        "a",
        "or",
        "dan",
        "yang",
        "untuk",
        "dari",
        "pada",
        "di",
        "ke",
        "final",
        "draft",
        "signed",
        "copy",
        "filed",
        "amended",
        "revised",
        "rev",
        "ver",
        "version",
        "update",
        "updated",
    }
)

_FILENAME_NOISE_RE = re.compile(
    r"\b(?:fy\s?20\d{2}|q[1-4]\s?20\d{2}|20\d{2}\s?q[1-4]|h[12]\s?20\d{2}|"
    r"20\d{2}[-_]\d{2}[-_]\d{2}|20\d{2}[-_]\d{2}|20\d{2}|"
    r"v(?:er(?:sion)?)?\.?\s?\d+(?:\.\d+)*|rev(?:ision)?\.?\s?\d+(?:\.\d+)*|"
    r"final|draft\s*\d*|signed|as\s+filed|copy|updated|revised|amended)\b",
    re.IGNORECASE,
)

_MONTH_MAP: dict[str, int] = {
    "jan": 1,
    "january": 1,
    "januari": 1,
    "feb": 2,
    "february": 2,
    "februari": 2,
    "mar": 3,
    "march": 3,
    "maret": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "mei": 5,
    "jun": 6,
    "june": 6,
    "juni": 6,
    "jul": 7,
    "july": 7,
    "juli": 7,
    "aug": 8,
    "august": 8,
    "agustus": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "oktober": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
    "desember": 12,
}


@dataclass(frozen=True)
class DocumentVersionSignal:
    """Compact workspace-scoped version signal attached to documents & retrieval hits."""

    group_id: UUID
    group_name: str
    relation_kind: str
    version_rank: int
    version_count: int
    is_latest: bool
    is_superseded: bool
    latest_document_id: UUID
    latest_filename: str
    latest_period_label: str | None
    version_note: str | None = None


@dataclass(frozen=True)
class VersionMemberInfo:
    document_id: UUID
    filename: str
    title: str | None
    period_label: str | None
    effective_date: str | None
    version_label: str | None
    version_rank: int
    is_latest: bool
    version_note: str | None
    linked_at: datetime
    created_at: datetime


@dataclass(frozen=True)
class VersionGroupDetail:
    group_id: UUID
    name: str
    relation_kind: str
    members: list[VersionMemberInfo]


@dataclass(frozen=True)
class VersionSuggestionDocSummary:
    document_id: UUID
    filename: str
    title: str | None
    doc_category: str | None
    doc_subtype: str | None
    period_label: str | None
    effective_date: str | None
    version_label: str | None
    primary_entities: list[str]
    created_at: datetime


@dataclass(frozen=True)
class VersionSuggestionDetail:
    id: UUID
    workspace_id: UUID
    document_a: VersionSuggestionDocSummary
    document_b: VersionSuggestionDocSummary
    suggested_newer_document_id: UUID
    suggested_relation: str
    confidence: float
    reasons: list[str]
    status: str
    created_at: datetime
    resolved_at: datetime | None


@dataclass(frozen=True)
class _CandidateDoc:
    document_id: UUID
    filename: str
    created_at: datetime
    profile: DocumentProfile | None


def _ordered_pair(doc_a: UUID, doc_b: UUID) -> tuple[UUID, UUID]:
    return (doc_a, doc_b) if doc_a < doc_b else (doc_b, doc_a)


def _normalize_identifier(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[\s\-_./]+", "", value.lower()).strip()


def _stripped_filename_stem(filename: str) -> str:
    stem = PurePosixPath(filename).stem
    cleaned = re.sub(r"[_\-]+", " ", stem)
    cleaned = _FILENAME_NOISE_RE.sub(" ", cleaned)
    cleaned = re.sub(r"[^\w\s]", " ", cleaned, flags=re.UNICODE)
    tokens = [tok for tok in cleaned.lower().split() if tok and tok not in _STOPWORDS]
    return " ".join(tokens)


def _entity_set(profile: DocumentProfile | None) -> set[str]:
    if profile is None or not isinstance(profile.primary_entities, list):
        return set()
    names: set[str] = set()
    for entry in profile.primary_entities:
        if not isinstance(entry, dict):
            continue
        norm = entry.get("normalized")
        if isinstance(norm, str) and norm.strip():
            names.add(norm.strip())
        name = entry.get("name")
        if isinstance(name, str) and name.strip():
            n = normalize_entity_name(name)
            if n:
                names.add(n)
        for alias in entry.get("aliases") or []:
            if isinstance(alias, str) and alias.strip():
                a = normalize_entity_name(alias)
                if len(a) >= 2:
                    names.add(a)
    return names


def _title_tokens(candidate: _CandidateDoc) -> set[str]:
    raw = (
        candidate.profile.title
        if candidate.profile is not None and candidate.profile.title
        else PurePosixPath(candidate.filename).stem
    )
    cleaned = re.sub(r"[_\-]+", " ", raw)
    cleaned = _FILENAME_NOISE_RE.sub(" ", cleaned)
    cleaned = re.sub(r"[^\w\s]", " ", cleaned, flags=re.UNICODE)
    return {
        tok
        for tok in cleaned.lower().split()
        if len(tok) >= 2 and tok not in _STOPWORDS and not tok.isdigit()
    }


def _parse_period_key(text: str | None) -> tuple[int, int] | None:
    if not text:
        return None
    s = text.replace("_", " ").strip()
    m = re.search(r"\b(20\d{2})[-/](\d{1,2})\b", s)
    if m:
        return (int(m.group(1)), min(max(int(m.group(2)), 1), 12))
    m = re.search(r"\bQ([1-4])\s*(20\d{2})\b", s, re.IGNORECASE)
    if m:
        return (int(m.group(2)), int(m.group(1)) * 3)
    m = re.search(r"\b(20\d{2})\s*Q([1-4])\b", s, re.IGNORECASE)
    if m:
        return (int(m.group(1)), int(m.group(2)) * 3)
    m = re.search(r"\bH([12])\s*(20\d{2})\b", s, re.IGNORECASE)
    if m:
        return (int(m.group(2)), int(m.group(1)) * 6)
    m = re.search(r"\b([A-Za-z]{3,10})\s+(20\d{2})\b", s)
    if m and m.group(1).lower() in _MONTH_MAP:
        return (int(m.group(2)), _MONTH_MAP[m.group(1).lower()])
    m = re.search(r"\b(?:FY\s*)?(20\d{2})\b", s, re.IGNORECASE)
    if m:
        return (int(m.group(1)), 12)
    return None


def _parse_version_key(text: str | None) -> tuple[int, tuple[int, ...]] | None:
    if not text:
        return None
    s = re.sub(r"[_\-]+", " ", text).strip().lower()
    m = re.search(
        r"\b(?:v(?:er(?:sion)?)?\.?\s*|rev(?:ision)?\.?\s*|amendment\s+|addendum\s+)(\d+(?:\.\d+)*)\b",
        s,
    )
    if m:
        parts = tuple(int(p) for p in m.group(1).split("."))
        return (2, parts)
    if "final" in s or "signed" in s or "as filed" in s:
        return (1, (1,))
    if "draft" in s:
        draft_num = re.search(r"draft\s*(\d+)", s)
        return (0, (int(draft_num.group(1)),) if draft_num else (0,))
    return None


def _pick_newer_document(a: _CandidateDoc, b: _CandidateDoc) -> UUID:
    a_date = a.profile.effective_date if a.profile else None
    b_date = b.profile.effective_date if b.profile else None
    if a_date and b_date and a_date != b_date:
        return a.document_id if a_date > b_date else b.document_id

    a_period = _parse_period_key(
        a.profile.period_label if a.profile else None
    ) or _parse_period_key(a.filename)
    b_period = _parse_period_key(
        b.profile.period_label if b.profile else None
    ) or _parse_period_key(b.filename)
    if a_period and b_period and a_period != b_period:
        return a.document_id if a_period > b_period else b.document_id

    a_ver = _parse_version_key(
        a.profile.version_label if a.profile else None
    ) or _parse_version_key(a.filename)
    b_ver = _parse_version_key(
        b.profile.version_label if b.profile else None
    ) or _parse_version_key(b.filename)
    if a_ver and b_ver and a_ver != b_ver:
        return a.document_id if a_ver > b_ver else b.document_id

    if a.created_at != b.created_at:
        return a.document_id if a.created_at > b.created_at else b.document_id
    return max(a.document_id, b.document_id)


def _score_candidate_pair(
    a: _CandidateDoc,
    b: _CandidateDoc,
) -> tuple[float, list[str], str]:
    """Return ``(confidence, reasons, suggested_relation)`` for a pair of docs."""
    reasons: list[str] = []
    score = 0.0

    num_a = _normalize_identifier(a.profile.document_number if a.profile else None)
    num_b = _normalize_identifier(b.profile.document_number if b.profile else None)
    same_doc_number = bool(num_a and len(num_a) >= 3 and num_a == num_b)

    entities_a = _entity_set(a.profile)
    entities_b = _entity_set(b.profile)
    shared_entities = entities_a & entities_b

    # Guard: if both documents identify primary entities and share none (and no exact
    # document number), they are about different subjects (e.g. Apple 10-K vs Microsoft 10-K).
    if entities_a and entities_b and not shared_entities and not same_doc_number:
        return 0.0, [], VERSION_RELATION_SUPERSEDES

    if same_doc_number:
        score += 0.55
        reasons.append(
            f"Matching document number ({a.profile.document_number if a.profile else num_a})"
        )

    if shared_entities:
        score += 0.30
        display_entity = next(iter(sorted(shared_entities)))
        if a.profile and isinstance(a.profile.primary_entities, list):
            for entry in a.profile.primary_entities:
                if isinstance(entry, dict) and entry.get("normalized") in shared_entities:
                    name = entry.get("name")
                    if isinstance(name, str) and name.strip():
                        display_entity = name.strip()
                        break
        reasons.append(f"Same primary entity ({display_entity})")

    stem_a = _stripped_filename_stem(a.filename)
    stem_b = _stripped_filename_stem(b.filename)
    stem_len = len(stem_a.replace(" ", ""))
    if (
        stem_a
        and stem_a == stem_b
        and stem_len >= 4
        and (stem_len >= 6 or shared_entities or same_doc_number)
    ):
        score += 0.35
        reasons.append("Matching base filename")

    cat_a = a.profile.doc_category if a.profile else "other"
    cat_b = b.profile.doc_category if b.profile else "other"
    sub_a = (a.profile.doc_subtype or "").strip().lower() if a.profile else ""
    sub_b = (b.profile.doc_subtype or "").strip().lower() if b.profile else ""
    if cat_a == cat_b and cat_a != "other":
        if sub_a and sub_a == sub_b:
            score += 0.20
            reasons.append(f"Same document type ({a.profile.doc_subtype if a.profile else cat_a})")
        else:
            score += 0.12
            reasons.append(f"Same category ({cat_a.replace('_', ' ')})")

    tokens_a = _title_tokens(a)
    tokens_b = _title_tokens(b)
    if tokens_a and tokens_b:
        union = tokens_a | tokens_b
        overlap = len(tokens_a & tokens_b) / len(union)
        if overlap >= 0.45:
            score += round(min(0.25, overlap * 0.25), 3)
            reasons.append("Similar document title")

    marker_a = (
        (a.profile.period_label if a.profile else None)
        or (a.profile.effective_date if a.profile else None)
        or (a.profile.version_label if a.profile else None)
        or str(_parse_period_key(a.filename) or _parse_version_key(a.filename) or "")
    )
    marker_b = (
        (b.profile.period_label if b.profile else None)
        or (b.profile.effective_date if b.profile else None)
        or (b.profile.version_label if b.profile else None)
        or str(_parse_period_key(b.filename) or _parse_version_key(b.filename) or "")
    )
    if marker_a and marker_b and marker_a.strip().lower() != marker_b.strip().lower():
        score += 0.15
        reasons.append("Different period or version marker")

    suggested_relation = (
        VERSION_RELATION_PERIODIC_SERIES
        if cat_a in _PERIODIC_CATEGORIES or cat_b in _PERIODIC_CATEGORIES
        else VERSION_RELATION_SUPERSEDES
    )
    return min(round(score, 3), 0.99), reasons, suggested_relation


async def detect_version_suggestions_for_document(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    document_id: UUID,
    commit: bool = True,
) -> list[DocumentVersionSuggestion]:
    """Detect and upsert pending version suggestions for one document inside one workspace."""
    rows = (
        await session.execute(
            select(
                DocumentWorkspace.document_id,
                DocumentWorkspace.filename,
                DocumentWorkspace.created_at,
                DocumentProfile,
            )
            .join(Document, Document.id == DocumentWorkspace.document_id)
            .outerjoin(
                DocumentProfile, DocumentProfile.document_id == DocumentWorkspace.document_id
            )
            .where(
                DocumentWorkspace.workspace_id == workspace_id,
                DocumentWorkspace.deleted_at.is_(None),
                Document.deleted_at.is_(None),
            )
        )
    ).all()

    target: _CandidateDoc | None = None
    others: list[_CandidateDoc] = []
    for doc_id, filename, created_at, profile in rows:
        candidate = _CandidateDoc(
            document_id=doc_id,
            filename=filename,
            created_at=created_at,
            profile=profile,
        )
        if doc_id == document_id:
            target = candidate
        else:
            others.append(candidate)

    if target is None or not others:
        return []

    members = (
        await session.execute(
            select(DocumentVersionMember.document_id, DocumentVersionMember.group_id).where(
                DocumentVersionMember.workspace_id == workspace_id
            )
        )
    ).all()
    doc_to_group = {doc_id: group_id for doc_id, group_id in members}
    target_group = doc_to_group.get(document_id)

    existing_rows = (
        await session.scalars(
            select(DocumentVersionSuggestion).where(
                DocumentVersionSuggestion.workspace_id == workspace_id,
                or_(
                    DocumentVersionSuggestion.document_a_id == document_id,
                    DocumentVersionSuggestion.document_b_id == document_id,
                ),
            )
        )
    ).all()
    existing_by_pair: dict[tuple[UUID, UUID], DocumentVersionSuggestion] = {
        (row.document_a_id, row.document_b_id): row for row in existing_rows
    }

    scored: list[tuple[float, _CandidateDoc, list[str], str]] = []
    for other in others:
        if target_group is not None and doc_to_group.get(other.document_id) == target_group:
            continue
        pair = _ordered_pair(document_id, other.document_id)
        existing = existing_by_pair.get(pair)
        if existing is not None and existing.status in (
            VERSION_SUGGESTION_DISMISSED,
            VERSION_SUGGESTION_CONFIRMED,
        ):
            continue
        confidence, reasons, relation = _score_candidate_pair(target, other)
        if confidence >= _SUGGESTION_THRESHOLD:
            scored.append((confidence, other, reasons, relation))

    scored.sort(key=lambda item: (-item[0], str(item[1].document_id)))
    top_matches = scored[:_MAX_SUGGESTIONS_PER_DOC]

    now = datetime.now(UTC)
    persisted: list[DocumentVersionSuggestion] = []
    for confidence, other, reasons, relation in top_matches:
        pair_a, pair_b = _ordered_pair(document_id, other.document_id)
        newer_id = _pick_newer_document(target, other)
        existing = existing_by_pair.get((pair_a, pair_b))
        if existing is not None:
            existing.confidence = confidence
            existing.reasons = reasons
            existing.suggested_newer_document_id = newer_id
            existing.suggested_relation = relation
            existing.updated_at = now
            persisted.append(existing)
        else:
            suggestion = DocumentVersionSuggestion(
                workspace_id=workspace_id,
                document_a_id=pair_a,
                document_b_id=pair_b,
                suggested_newer_document_id=newer_id,
                suggested_relation=relation,
                confidence=confidence,
                reasons=reasons,
                status=VERSION_SUGGESTION_PENDING,
                created_at=now,
                updated_at=now,
            )
            session.add(suggestion)
            persisted.append(suggestion)

    if commit:
        await session.commit()
    else:
        await session.flush()
    return persisted


async def _require_live_workspace_document(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    document_id: UUID,
) -> tuple[DocumentWorkspace, DocumentProfile | None]:
    row = (
        await session.execute(
            select(DocumentWorkspace, DocumentProfile)
            .join(Document, Document.id == DocumentWorkspace.document_id)
            .outerjoin(
                DocumentProfile, DocumentProfile.document_id == DocumentWorkspace.document_id
            )
            .where(
                DocumentWorkspace.workspace_id == workspace_id,
                DocumentWorkspace.document_id == document_id,
                DocumentWorkspace.deleted_at.is_(None),
                Document.deleted_at.is_(None),
            )
        )
    ).first()
    if row is None:
        raise AppError(
            status_code=404,
            code="document_not_found",
            title="Document not found",
            detail="The requested document does not exist in this workspace.",
        )
    return row[0], row[1]


async def _normalize_group_ranks(
    session: AsyncSession, *, group_id: UUID
) -> list[DocumentVersionMember]:
    members = list(
        (
            await session.scalars(
                select(DocumentVersionMember)
                .where(DocumentVersionMember.group_id == group_id)
                .order_by(
                    DocumentVersionMember.version_rank.asc(),
                    DocumentVersionMember.linked_at.asc(),
                    DocumentVersionMember.document_id.asc(),
                )
            )
        ).all()
    )
    total = len(members)
    for idx, member in enumerate(members, start=1):
        member.version_rank = idx
        member.is_latest = idx == total
    return members


async def _mark_group_suggestions_confirmed(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    member_doc_ids: Sequence[UUID],
    actor_user_id: UUID | None,
) -> None:
    if len(member_doc_ids) < 2:
        return
    now = datetime.now(UTC)
    pending = (
        await session.scalars(
            select(DocumentVersionSuggestion).where(
                DocumentVersionSuggestion.workspace_id == workspace_id,
                DocumentVersionSuggestion.status == VERSION_SUGGESTION_PENDING,
                DocumentVersionSuggestion.document_a_id.in_(member_doc_ids),
                DocumentVersionSuggestion.document_b_id.in_(member_doc_ids),
            )
        )
    ).all()
    for suggestion in pending:
        suggestion.status = VERSION_SUGGESTION_CONFIRMED
        suggestion.resolved_by_user_id = actor_user_id
        suggestion.resolved_at = now
        suggestion.updated_at = now


async def link_document_versions(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    newer_document_id: UUID,
    older_document_id: UUID,
    relation_kind: str | None = None,
    group_name: str | None = None,
    version_note: str | None = None,
    actor_user_id: UUID | None = None,
    commit: bool = True,
) -> DocumentVersionGroup:
    """Link two workspace documents into a version group with ``newer`` ranked above ``older``."""
    if newer_document_id == older_document_id:
        raise AppError(
            status_code=422,
            code="invalid_version_link",
            title="Invalid version link",
            detail="A document cannot be linked as a version of itself.",
        )
    if relation_kind is not None and relation_kind not in VERSION_RELATION_KINDS:
        raise AppError(
            status_code=422,
            code="invalid_relation_kind",
            title="Invalid version relation",
            detail="Relation kind must be 'supersedes' or 'periodic_series'.",
        )

    newer_link, newer_profile = await _require_live_workspace_document(
        session, workspace_id=workspace_id, document_id=newer_document_id
    )
    await _require_live_workspace_document(
        session, workspace_id=workspace_id, document_id=older_document_id
    )

    newer_member = await session.get(DocumentVersionMember, (workspace_id, newer_document_id))
    older_member = await session.get(DocumentVersionMember, (workspace_id, older_document_id))

    now = datetime.now(UTC)
    effective_relation = relation_kind or (
        VERSION_RELATION_PERIODIC_SERIES
        if newer_profile is not None and newer_profile.doc_category in _PERIODIC_CATEGORIES
        else VERSION_RELATION_SUPERSEDES
    )
    default_name = (
        (group_name.strip()[:255] if group_name and group_name.strip() else None)
        or (newer_profile.title if newer_profile and newer_profile.title else None)
        or newer_link.filename[:255]
    )

    if newer_member is None and older_member is None:
        group = DocumentVersionGroup(
            workspace_id=workspace_id,
            name=default_name,
            relation_kind=effective_relation,
            created_by_user_id=actor_user_id,
            created_at=now,
            updated_at=now,
        )
        session.add(group)
        await session.flush()
        session.add(
            DocumentVersionMember(
                workspace_id=workspace_id,
                document_id=older_document_id,
                group_id=group.id,
                version_rank=1,
                is_latest=False,
                linked_by_user_id=actor_user_id,
                linked_at=now,
            )
        )
        session.add(
            DocumentVersionMember(
                workspace_id=workspace_id,
                document_id=newer_document_id,
                group_id=group.id,
                version_rank=2,
                is_latest=True,
                version_note=version_note.strip()[:255]
                if version_note and version_note.strip()
                else None,
                linked_by_user_id=actor_user_id,
                linked_at=now,
            )
        )
    elif newer_member is None and older_member is not None:
        loaded_group = await session.get(DocumentVersionGroup, older_member.group_id)
        assert loaded_group is not None
        group = loaded_group
        if group_name and group_name.strip():
            group.name = group_name.strip()[:255]
        if relation_kind is not None:
            group.relation_kind = relation_kind
        group.updated_at = now
        existing_members = await _normalize_group_ranks(session, group_id=group.id)
        max_rank = existing_members[-1].version_rank if existing_members else 0
        session.add(
            DocumentVersionMember(
                workspace_id=workspace_id,
                document_id=newer_document_id,
                group_id=group.id,
                version_rank=max_rank + 1,
                is_latest=True,
                version_note=version_note.strip()[:255]
                if version_note and version_note.strip()
                else None,
                linked_by_user_id=actor_user_id,
                linked_at=now,
            )
        )
    elif newer_member is not None and older_member is None:
        loaded_group = await session.get(DocumentVersionGroup, newer_member.group_id)
        assert loaded_group is not None
        group = loaded_group
        if group_name and group_name.strip():
            group.name = group_name.strip()[:255]
        if relation_kind is not None:
            group.relation_kind = relation_kind
        group.updated_at = now
        existing_members = await _normalize_group_ranks(session, group_id=group.id)
        insert_rank = newer_member.version_rank
        for member in existing_members:
            if member.version_rank >= insert_rank:
                member.version_rank += 1
        if version_note and version_note.strip():
            newer_member.version_note = version_note.strip()[:255]
        session.add(
            DocumentVersionMember(
                workspace_id=workspace_id,
                document_id=older_document_id,
                group_id=group.id,
                version_rank=insert_rank,
                is_latest=False,
                linked_by_user_id=actor_user_id,
                linked_at=now,
            )
        )
    else:
        assert newer_member is not None and older_member is not None
        loaded_group = await session.get(DocumentVersionGroup, newer_member.group_id)
        assert loaded_group is not None
        group = loaded_group
        if group_name and group_name.strip():
            group.name = group_name.strip()[:255]
        if relation_kind is not None:
            group.relation_kind = relation_kind
        group.updated_at = now
        if version_note and version_note.strip():
            newer_member.version_note = version_note.strip()[:255]

        if newer_member.group_id == older_member.group_id:
            if newer_member.version_rank < older_member.version_rank:
                newer_member.version_rank, older_member.version_rank = (
                    older_member.version_rank,
                    newer_member.version_rank,
                )
        else:
            old_group_id = older_member.group_id
            old_members = await _normalize_group_ranks(session, group_id=old_group_id)
            new_members = await _normalize_group_ranks(session, group_id=group.id)
            offset = len(old_members)
            for member in new_members:
                member.version_rank += offset
            for idx, member in enumerate(old_members, start=1):
                member.group_id = group.id
                member.version_rank = idx
                member.is_latest = False
            await session.flush()
            await session.execute(
                delete(DocumentVersionGroup).where(DocumentVersionGroup.id == old_group_id)
            )

    await session.flush()
    final_members = await _normalize_group_ranks(session, group_id=group.id)
    await _mark_group_suggestions_confirmed(
        session,
        workspace_id=workspace_id,
        member_doc_ids=[m.document_id for m in final_members],
        actor_user_id=actor_user_id,
    )

    if commit:
        await session.commit()
    else:
        await session.flush()
    return group


async def confirm_suggestion(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    suggestion_id: UUID,
    actor_user_id: UUID | None = None,
    relation_kind: str | None = None,
    newer_document_id: UUID | None = None,
    group_name: str | None = None,
    commit: bool = True,
) -> tuple[DocumentVersionSuggestion, DocumentVersionGroup]:
    """Confirm a pending suggestion and link the two documents in a version group."""
    suggestion = await session.scalar(
        select(DocumentVersionSuggestion).where(
            DocumentVersionSuggestion.id == suggestion_id,
            DocumentVersionSuggestion.workspace_id == workspace_id,
        )
    )
    if suggestion is None:
        raise AppError(
            status_code=404,
            code="version_suggestion_not_found",
            title="Version suggestion not found",
            detail="The requested version suggestion does not exist in this workspace.",
        )
    if suggestion.status != VERSION_SUGGESTION_PENDING:
        raise AppError(
            status_code=409,
            code="version_suggestion_already_resolved",
            title="Version suggestion already resolved",
            detail=f"This suggestion is already {suggestion.status}.",
        )

    chosen_newer = newer_document_id or suggestion.suggested_newer_document_id
    if chosen_newer not in (suggestion.document_a_id, suggestion.document_b_id):
        raise AppError(
            status_code=422,
            code="invalid_newer_document",
            title="Invalid newer document",
            detail="newer_document_id must be one of the two suggested documents.",
        )
    chosen_older = (
        suggestion.document_b_id
        if chosen_newer == suggestion.document_a_id
        else suggestion.document_a_id
    )
    chosen_relation = relation_kind or suggestion.suggested_relation

    group = await link_document_versions(
        session,
        workspace_id=workspace_id,
        newer_document_id=chosen_newer,
        older_document_id=chosen_older,
        relation_kind=chosen_relation,
        group_name=group_name,
        actor_user_id=actor_user_id,
        commit=False,
    )
    now = datetime.now(UTC)
    suggestion.status = VERSION_SUGGESTION_CONFIRMED
    suggestion.resolved_by_user_id = actor_user_id
    suggestion.resolved_at = now
    suggestion.updated_at = now
    if commit:
        await session.commit()
    else:
        await session.flush()
    return suggestion, group


async def dismiss_suggestion(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    suggestion_id: UUID,
    actor_user_id: UUID | None = None,
    commit: bool = True,
) -> DocumentVersionSuggestion:
    """Dismiss a pending suggestion so it is not proposed again."""
    suggestion = await session.scalar(
        select(DocumentVersionSuggestion).where(
            DocumentVersionSuggestion.id == suggestion_id,
            DocumentVersionSuggestion.workspace_id == workspace_id,
        )
    )
    if suggestion is None:
        raise AppError(
            status_code=404,
            code="version_suggestion_not_found",
            title="Version suggestion not found",
            detail="The requested version suggestion does not exist in this workspace.",
        )
    now = datetime.now(UTC)
    suggestion.status = VERSION_SUGGESTION_DISMISSED
    suggestion.resolved_by_user_id = actor_user_id
    suggestion.resolved_at = now
    suggestion.updated_at = now
    if commit:
        await session.commit()
    else:
        await session.flush()
    return suggestion


async def unlink_document_version(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    document_id: UUID,
    commit: bool = True,
) -> bool:
    """Remove a document from its version group, dissolving the group if <= 1 doc remains."""
    member = await session.get(DocumentVersionMember, (workspace_id, document_id))
    if member is None:
        return False
    group_id = member.group_id
    await session.delete(member)
    await session.flush()

    remaining = list(
        (
            await session.scalars(
                select(DocumentVersionMember)
                .where(DocumentVersionMember.group_id == group_id)
                .order_by(
                    DocumentVersionMember.version_rank.asc(),
                    DocumentVersionMember.linked_at.asc(),
                )
            )
        ).all()
    )
    if len(remaining) <= 1:
        for leftover in remaining:
            await session.delete(leftover)
        await session.flush()
        await session.execute(
            delete(DocumentVersionGroup).where(DocumentVersionGroup.id == group_id)
        )
    else:
        await _normalize_group_ranks(session, group_id=group_id)

    if commit:
        await session.commit()
    else:
        await session.flush()
    return True


async def update_version_group(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    group_id: UUID,
    name: str | None = None,
    relation_kind: str | None = None,
    member_order: Sequence[UUID] | None = None,
    commit: bool = True,
) -> DocumentVersionGroup:
    """Update a version group's name, relation kind, or oldest-to-newest member ordering."""
    group = await session.scalar(
        select(DocumentVersionGroup).where(
            DocumentVersionGroup.id == group_id,
            DocumentVersionGroup.workspace_id == workspace_id,
        )
    )
    if group is None:
        raise AppError(
            status_code=404,
            code="version_group_not_found",
            title="Version group not found",
            detail="The requested version group does not exist in this workspace.",
        )
    if relation_kind is not None:
        if relation_kind not in VERSION_RELATION_KINDS:
            raise AppError(
                status_code=422,
                code="invalid_relation_kind",
                title="Invalid version relation",
                detail="Relation kind must be 'supersedes' or 'periodic_series'.",
            )
        group.relation_kind = relation_kind
    if name is not None:
        cleaned_name = name.strip()[:255]
        if not cleaned_name:
            raise AppError(
                status_code=422,
                code="invalid_group_name",
                title="Invalid group name",
                detail="Version group name cannot be blank.",
            )
        group.name = cleaned_name

    if member_order is not None:
        members = list(
            (
                await session.scalars(
                    select(DocumentVersionMember).where(DocumentVersionMember.group_id == group_id)
                )
            ).all()
        )
        member_by_doc = {m.document_id: m for m in members}
        if len(member_order) != len(members) or set(member_order) != set(member_by_doc.keys()):
            raise AppError(
                status_code=422,
                code="invalid_member_order",
                title="Invalid member order",
                detail=(
                    "member_order must list every document currently in this version group once."
                ),
            )
        total = len(member_order)
        for idx, doc_id in enumerate(member_order, start=1):
            member = member_by_doc[doc_id]
            member.version_rank = idx
            member.is_latest = idx == total

    group.updated_at = datetime.now(UTC)
    if commit:
        await session.commit()
    else:
        await session.flush()
    return group


async def cleanup_document_versions_on_unlink(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    document_id: UUID,
) -> None:
    """Clean up workspace version memberships & suggestions when a document is deleted."""
    await session.execute(
        delete(DocumentVersionSuggestion).where(
            DocumentVersionSuggestion.workspace_id == workspace_id,
            or_(
                DocumentVersionSuggestion.document_a_id == document_id,
                DocumentVersionSuggestion.document_b_id == document_id,
            ),
        )
    )
    await unlink_document_version(
        session,
        workspace_id=workspace_id,
        document_id=document_id,
        commit=False,
    )


async def load_workspace_version_signals(
    session: AsyncSession,
    *,
    workspace_ids: Sequence[UUID],
    document_ids: Sequence[UUID],
) -> dict[tuple[UUID, UUID], DocumentVersionSignal]:
    """Batch-load ``DocumentVersionSignal`` keyed by ``(workspace_id, document_id)``."""
    ws_ids = list(dict.fromkeys(workspace_ids))
    doc_ids = list(dict.fromkeys(document_ids))
    if not ws_ids or not doc_ids:
        return {}

    target_rows = (
        await session.execute(
            select(DocumentVersionMember, DocumentVersionGroup)
            .join(DocumentVersionGroup, DocumentVersionGroup.id == DocumentVersionMember.group_id)
            .where(
                DocumentVersionMember.workspace_id.in_(ws_ids),
                DocumentVersionMember.document_id.in_(doc_ids),
            )
        )
    ).all()
    if not target_rows:
        return {}

    group_ids = list({group.id for _, group in target_rows})
    all_members = (
        await session.execute(
            select(
                DocumentVersionMember.group_id,
                DocumentVersionMember.document_id,
                DocumentVersionMember.is_latest,
                DocumentVersionMember.version_rank,
                DocumentWorkspace.filename,
                DocumentProfile.period_label,
            )
            .join(
                DocumentWorkspace,
                (DocumentWorkspace.workspace_id == DocumentVersionMember.workspace_id)
                & (DocumentWorkspace.document_id == DocumentVersionMember.document_id),
            )
            .outerjoin(
                DocumentProfile, DocumentProfile.document_id == DocumentVersionMember.document_id
            )
            .where(DocumentVersionMember.group_id.in_(group_ids))
            .order_by(DocumentVersionMember.version_rank.asc())
        )
    ).all()

    counts: dict[UUID, int] = defaultdict(int)
    latest_by_group: dict[UUID, tuple[UUID, str, str | None]] = {}
    for g_id, d_id, is_latest, _, filename, period_label in all_members:
        counts[g_id] += 1
        if is_latest or g_id not in latest_by_group:
            latest_by_group[g_id] = (d_id, filename, period_label)

    signals: dict[tuple[UUID, UUID], DocumentVersionSignal] = {}
    for member, group in target_rows:
        latest_doc_id, latest_filename, latest_period = latest_by_group.get(
            group.id, (member.document_id, "", None)
        )
        is_superseded = group.relation_kind == VERSION_RELATION_SUPERSEDES and not member.is_latest
        signals[(member.workspace_id, member.document_id)] = DocumentVersionSignal(
            group_id=group.id,
            group_name=group.name,
            relation_kind=group.relation_kind,
            version_rank=member.version_rank,
            version_count=counts.get(group.id, 1),
            is_latest=member.is_latest,
            is_superseded=is_superseded,
            latest_document_id=latest_doc_id,
            latest_filename=latest_filename,
            latest_period_label=latest_period,
            version_note=member.version_note,
        )
    return signals


async def load_document_version_signals_for_workspace(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    document_ids: Sequence[UUID],
) -> dict[UUID, DocumentVersionSignal]:
    by_pair = await load_workspace_version_signals(
        session, workspace_ids=[workspace_id], document_ids=document_ids
    )
    return {doc_id: sig for (_, doc_id), sig in by_pair.items()}


async def load_pending_suggestion_counts(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    document_ids: Sequence[UUID] | None = None,
) -> dict[UUID, int]:
    """Return ``document_id -> pending suggestion count`` inside one workspace."""
    statement = select(
        DocumentVersionSuggestion.document_a_id,
        DocumentVersionSuggestion.document_b_id,
    ).where(
        DocumentVersionSuggestion.workspace_id == workspace_id,
        DocumentVersionSuggestion.status == VERSION_SUGGESTION_PENDING,
    )
    if document_ids is not None:
        doc_set = list(dict.fromkeys(document_ids))
        if not doc_set:
            return {}
        statement = statement.where(
            or_(
                DocumentVersionSuggestion.document_a_id.in_(doc_set),
                DocumentVersionSuggestion.document_b_id.in_(doc_set),
            )
        )
    rows = (await session.execute(statement)).all()
    counts: dict[UUID, int] = defaultdict(int)
    filter_set = set(document_ids) if document_ids is not None else None
    for doc_a, doc_b in rows:
        if filter_set is None or doc_a in filter_set:
            counts[doc_a] += 1
        if filter_set is None or doc_b in filter_set:
            counts[doc_b] += 1
    return dict(counts)


def _suggestion_doc_summary(
    link: DocumentWorkspace,
    profile: DocumentProfile | None,
) -> VersionSuggestionDocSummary:
    entities: list[str] = []
    if profile is not None and isinstance(profile.primary_entities, list):
        for entry in profile.primary_entities:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                entities.append(entry["name"])
    return VersionSuggestionDocSummary(
        document_id=link.document_id,
        filename=link.filename,
        title=profile.title if profile else None,
        doc_category=profile.doc_category if profile else None,
        doc_subtype=profile.doc_subtype if profile else None,
        period_label=profile.period_label if profile else None,
        effective_date=profile.effective_date if profile else None,
        version_label=profile.version_label if profile else None,
        primary_entities=entities,
        created_at=link.created_at,
    )


async def list_workspace_suggestions(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    status: str = VERSION_SUGGESTION_PENDING,
    document_id: UUID | None = None,
) -> list[VersionSuggestionDetail]:
    """List version suggestions in a workspace enriched with document metadata."""
    statement = (
        select(DocumentVersionSuggestion)
        .where(
            DocumentVersionSuggestion.workspace_id == workspace_id,
            DocumentVersionSuggestion.status == status,
        )
        .order_by(
            DocumentVersionSuggestion.confidence.desc(),
            DocumentVersionSuggestion.created_at.desc(),
        )
    )
    if document_id is not None:
        statement = statement.where(
            or_(
                DocumentVersionSuggestion.document_a_id == document_id,
                DocumentVersionSuggestion.document_b_id == document_id,
            )
        )
    suggestions = list((await session.scalars(statement)).all())
    if not suggestions:
        return []

    doc_ids = list({s.document_a_id for s in suggestions} | {s.document_b_id for s in suggestions})
    rows = (
        await session.execute(
            select(DocumentWorkspace, DocumentProfile)
            .join(Document, Document.id == DocumentWorkspace.document_id)
            .outerjoin(
                DocumentProfile, DocumentProfile.document_id == DocumentWorkspace.document_id
            )
            .where(
                DocumentWorkspace.workspace_id == workspace_id,
                DocumentWorkspace.document_id.in_(doc_ids),
                DocumentWorkspace.deleted_at.is_(None),
                Document.deleted_at.is_(None),
            )
        )
    ).all()
    summaries = {link.document_id: _suggestion_doc_summary(link, profile) for link, profile in rows}

    details: list[VersionSuggestionDetail] = []
    for s in suggestions:
        doc_a = summaries.get(s.document_a_id)
        doc_b = summaries.get(s.document_b_id)
        if doc_a is None or doc_b is None:
            continue
        details.append(
            VersionSuggestionDetail(
                id=s.id,
                workspace_id=s.workspace_id,
                document_a=doc_a,
                document_b=doc_b,
                suggested_newer_document_id=s.suggested_newer_document_id,
                suggested_relation=s.suggested_relation,
                confidence=s.confidence,
                reasons=list(s.reasons or []),
                status=s.status,
                created_at=s.created_at,
                resolved_at=s.resolved_at,
            )
        )
    return details


async def load_document_version_detail(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    document_id: UUID,
) -> tuple[VersionGroupDetail | None, list[VersionSuggestionDetail]]:
    """Return a document's confirmed version group (if any) and pending suggestions."""
    await _require_live_workspace_document(
        session, workspace_id=workspace_id, document_id=document_id
    )
    member = await session.get(DocumentVersionMember, (workspace_id, document_id))
    group_detail: VersionGroupDetail | None = None
    if member is not None:
        group = await session.get(DocumentVersionGroup, member.group_id)
        if group is not None:
            rows = (
                await session.execute(
                    select(DocumentVersionMember, DocumentWorkspace, DocumentProfile)
                    .join(
                        DocumentWorkspace,
                        (DocumentWorkspace.workspace_id == DocumentVersionMember.workspace_id)
                        & (DocumentWorkspace.document_id == DocumentVersionMember.document_id),
                    )
                    .outerjoin(
                        DocumentProfile,
                        DocumentProfile.document_id == DocumentVersionMember.document_id,
                    )
                    .where(
                        DocumentVersionMember.group_id == group.id,
                        DocumentWorkspace.deleted_at.is_(None),
                    )
                    .order_by(DocumentVersionMember.version_rank.asc())
                )
            ).all()
            members = [
                VersionMemberInfo(
                    document_id=m.document_id,
                    filename=link.filename,
                    title=profile.title if profile else None,
                    period_label=profile.period_label if profile else None,
                    effective_date=profile.effective_date if profile else None,
                    version_label=profile.version_label if profile else None,
                    version_rank=m.version_rank,
                    is_latest=m.is_latest,
                    version_note=m.version_note,
                    linked_at=m.linked_at,
                    created_at=link.created_at,
                )
                for m, link, profile in rows
            ]
            group_detail = VersionGroupDetail(
                group_id=group.id,
                name=group.name,
                relation_kind=group.relation_kind,
                members=members,
            )

    suggestions = await list_workspace_suggestions(
        session,
        workspace_id=workspace_id,
        status=VERSION_SUGGESTION_PENDING,
        document_id=document_id,
    )
    return group_detail, suggestions
