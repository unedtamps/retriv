"""Automatic document intelligence extraction and canonical profiling.

When a document finishes extraction (prose or tabular), Retriv builds a compact
digest of its opening pages, outline headings, and closing block (or sheet
schema + sample rows for tabular files) and extracts structured metadata using
the deployment's configured ``AgentModelSetting``.

If no chat model is configured yet, or if the model call fails or returns
malformed output, extraction falls back cleanly to a deterministic ``basic``
heuristic profile so ingestion never breaks.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.chat import ChatProviderError, OpenAICompatibleChatProvider
from app.agents.model_settings import (
    AgentModelConfig,
    fresh_session_id,
    load_agent_model_config,
    provider_for,
)
from app.db.models import (
    PROFILE_STATUS_BASIC,
    PROFILE_STATUS_ENRICHED,
    Document,
    DocumentChunk,
    DocumentProfile,
    DocumentRow,
    DocumentWorkspace,
    ProcessingRun,
)

logger = logging.getLogger(__name__)

DOC_CATEGORIES: tuple[str, ...] = (
    "financial_report",
    "contract_agreement",
    "policy_sop",
    "regulation_legal",
    "presentation_deck",
    "invoice_receipt",
    "meeting_memo",
    "technical_spec",
    "tabular_dataset",
    "other",
)
DOC_CATEGORY_SET = frozenset(DOC_CATEGORIES)

_PREFIX_LEGAL_TOKENS = frozenset({"pt", "cv", "ud", "pd", "perum", "yayasan", "koperasi"})
_SUFFIX_LEGAL_TOKENS = frozenset(
    {
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "ltd",
        "limited",
        "llc",
        "llp",
        "plc",
        "co",
        "company",
        "tbk",
        "persero",
        "gmbh",
        "ag",
        "sa",
        "nv",
        "bv",
        "pte",
        "sdn",
        "bhd",
        "holdings",
        "holding",
        "group",
    }
)

_MAX_OPENING_TOKENS = 2000
_MAX_OPENING_CHARS = 8000
_MAX_CLOSING_CHARS = 1600
_MAX_HEADINGS = 60
_MAX_TABULAR_SHEETS = 4
_MAX_TABULAR_SAMPLE_ROWS = 5
_MAX_WORKSPACE_ENTITIES = 30

_PERIOD_RE = re.compile(
    r"\b(?:FY\s?20\d{2}|Q[1-4]\s?20\d{2}|20\d{2}\s?Q[1-4]|H[12]\s?20\d{2}|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+20\d{2}|"
    r"20\d{2})\b",
    re.IGNORECASE,
)
_VERSION_RE = re.compile(
    r"\b(?:v(?:er(?:sion)?)?\.?\s?\d+(?:\.\d+)*|rev(?:ision)?\.?\s?\d+(?:\.\d+)*|"
    r"amendment\s+\d+|addendum\s+\d+|draft\s*\d*|final)\b",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_SYSTEM_PROMPT = (
    "You extract structured metadata from a document digest for an enterprise retrieval system.\n"
    "Return ONLY a single JSON object (no markdown fences, no commentary) with these exact keys:\n"
    "{\n"
    '  "title": string or null,\n'
    '  "doc_category": one of ["financial_report", "contract_agreement", "policy_sop", '
    '"regulation_legal", "presentation_deck", "invoice_receipt", "meeting_memo", '
    '"technical_spec", "tabular_dataset", "other"],\n'
    '  "doc_subtype": short string or null (e.g. "Form 10-K", "Master Services Agreement", '
    '"HR Leave Policy"),\n'
    '  "language": ISO-639-1 lowercase code (e.g. "en", "id") or null,\n'
    '  "primary_entities": [{"name": string, '
    '"type": "company"|"org"|"person"|"product"|"regulator", "aliases": [string]}],\n'
    '  "counterparties": [{"name": string, '
    '"type": "company"|"org"|"person"|"product"|"regulator", "aliases": [string]}],\n'
    '  "period_label": string or null (e.g. "FY2025", "Q3 2025", "2025-09", "2025"),\n'
    '  "effective_date": "YYYY-MM-DD" or null,\n'
    '  "document_number": string or null (e.g. "SOP-HR-014", "MSA-2025-009", "INV-1042"),\n'
    '  "version_label": string or null (e.g. "v2.1", "Rev 3", "As Filed", "Final", "Draft"),\n'
    '  "summary": 1-2 concise sentences describing what this document is and covers,\n'
    '  "topics": [up to 8 concise lowercase topic keywords]\n'
    "}\n"
    "Rules:\n"
    "- Identify entities from the document content itself, never blindly from an opaque filename.\n"
    "- For filings/reports about a company (e.g. Form 10-K for Apple Inc.), put the subject "
    "company in primary_entities (with common aliases like tickers or short names).\n"
    "- For contracts/invoices between two parties, put the primary issuer/subject in "
    "primary_entities and the other party in counterparties.\n"
    "- Reuse an exact name from the workspace entity vocabulary when referring to the same org.\n"
    "- Use null or [] when a field is genuinely absent; never fabricate identifiers or dates."
)


def normalize_entity_name(name: str) -> str:
    """Normalize an entity name for matching across filings and documents."""
    cleaned = re.sub(r"[^\w\s]", " ", name.lower(), flags=re.UNICODE)
    tokens = [token for token in cleaned.split() if token]
    if not tokens:
        return ""
    stripped = list(tokens)
    while len(stripped) > 1 and stripped[0] in _PREFIX_LEGAL_TOKENS:
        stripped.pop(0)
    while len(stripped) > 1 and stripped[-1] in _SUFFIX_LEGAL_TOKENS:
        stripped.pop()
    return " ".join(stripped) if stripped else " ".join(tokens)


def normalize_entity_entries(raw: Any, *, max_items: int = 8) -> list[dict[str, Any]]:
    """Validate and deduplicate LLM-extracted entity entries."""
    if not isinstance(raw, list):
        return []
    allowed_types = {"company", "org", "person", "product", "regulator"}
    seen: set[str] = set()
    entries: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            name = item.strip()
            entity_type = "org"
            aliases_raw: list[Any] = []
        elif isinstance(item, dict):
            name_val = item.get("name")
            if not isinstance(name_val, str):
                continue
            name = name_val.strip()
            raw_type = item.get("type")
            entity_type = (
                raw_type.strip().lower()
                if isinstance(raw_type, str) and raw_type.strip().lower() in allowed_types
                else "org"
            )
            raw_aliases = item.get("aliases")
            aliases_raw = raw_aliases if isinstance(raw_aliases, list) else []
        else:
            continue
        if not name:
            continue
        name = name[:160]
        normalized = normalize_entity_name(name)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        aliases: list[str] = []
        alias_seen = {name.lower(), normalized}
        for alias in aliases_raw:
            if not isinstance(alias, str):
                continue
            trimmed = alias.strip()[:120]
            if trimmed and trimmed.lower() not in alias_seen:
                aliases.append(trimmed)
                alias_seen.add(trimmed.lower())
            if len(aliases) >= 6:
                break
        entries.append(
            {
                "name": name,
                "normalized": normalized,
                "type": entity_type,
                "aliases": aliases,
            }
        )
        if len(entries) >= max_items:
            break
    return entries


def build_search_text(
    *,
    title: str | None,
    doc_category: str,
    doc_subtype: str | None,
    primary_entities: list[dict[str, Any]],
    counterparties: list[dict[str, Any]],
    period_label: str | None,
    effective_date: str | None,
    document_number: str | None,
    version_label: str | None,
    topics: list[str],
    summary: str | None,
) -> str:
    """Flatten structured profile fields into a lowercase search string."""
    parts: list[str] = []
    for value in (
        title,
        doc_category.replace("_", " "),
        doc_subtype,
        period_label,
        effective_date,
        document_number,
        version_label,
    ):
        if value and value.strip():
            parts.append(value.strip())
    for group in (primary_entities, counterparties):
        for entry in group:
            name = entry.get("name")
            if isinstance(name, str) and name.strip():
                parts.append(name.strip())
            normalized = entry.get("normalized")
            if isinstance(normalized, str) and normalized.strip():
                parts.append(normalized.strip())
            for alias in entry.get("aliases") or []:
                if isinstance(alias, str) and alias.strip():
                    parts.append(alias.strip())
    for topic in topics:
        if topic.strip():
            parts.append(topic.strip())
    if summary and summary.strip():
        parts.append(summary.strip())

    # Deduplicate consecutive / identical lowered fragments while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for part in parts:
        lowered = re.sub(r"\s+", " ", part.lower()).strip()
        if lowered and lowered not in seen:
            seen.add(lowered)
            unique.append(lowered)
    return " ".join(unique)


def _clean_filename_title(filename: str) -> str:
    stem = PurePosixPath(filename).stem
    cleaned = re.sub(r"[_\-]+", " ", stem)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return (cleaned or filename)[:255]


def _has_any_term(haystack: str, terms: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(term)}\b", haystack) is not None for term in terms)


def _heuristic_category(filename: str, sample_text: str, *, is_tabular: bool) -> str:
    if is_tabular:
        return "tabular_dataset"
    haystack = f"{filename} {sample_text[:1500]}".lower().replace("_", " ")
    if _has_any_term(
        haystack,
        (
            "10-k",
            "10-q",
            "annual report",
            "quarterly report",
            "financial statements",
            "balance sheet",
            "income statement",
            "laporan keuangan",
            "laporan tahunan",
        ),
    ):
        return "financial_report"
    if _has_any_term(
        haystack,
        (
            "invoice",
            "receipt",
            "faktur",
            "kwitansi",
            "purchase order",
            "billing statement",
        ),
    ):
        return "invoice_receipt"
    if _has_any_term(
        haystack,
        (
            "master services agreement",
            "non-disclosure",
            "nda",
            "contract",
            "agreement",
            "perjanjian",
            "memorandum of understanding",
            "addendum",
            "amendment",
        ),
    ):
        return "contract_agreement"
    if _has_any_term(
        haystack,
        (
            "standard operating procedure",
            "sop",
            "policy",
            "handbook",
            "guideline",
            "kebijakan",
            "prosedur",
        ),
    ):
        return "policy_sop"
    if _has_any_term(
        haystack,
        (
            "regulation",
            "statute",
            "decree",
            "peraturan",
            "undang-undang",
            "compliance",
        ),
    ):
        return "regulation_legal"
    if _has_any_term(
        haystack,
        ("minutes of meeting", "meeting notes", "notulen", "memo", "memorandum"),
    ):
        return "meeting_memo"
    if _has_any_term(
        haystack,
        ("architecture", "specification", "rfc", "api reference", "runbook", "design doc"),
    ):
        return "technical_spec"
    if filename.lower().endswith((".pptx", ".ppt", ".key")):
        return "presentation_deck"
    return "other"


def _build_basic_profile_fields(
    *,
    filename: str,
    digest: str,
    is_tabular: bool,
    opening_snippet: str | None = None,
) -> dict[str, Any]:
    title = _clean_filename_title(filename)
    category = _heuristic_category(filename, digest, is_tabular=is_tabular)
    normalized_filename = re.sub(r"[_\-]+", " ", PurePosixPath(filename).stem)
    combined = f"{normalized_filename}\n{digest[:1200]}"
    period_match = _PERIOD_RE.search(combined)
    version_match = _VERSION_RE.search(normalized_filename) or _VERSION_RE.search(digest[:600])
    summary = None
    if opening_snippet:
        collapsed = re.sub(r"\s+", " ", opening_snippet).strip()
        if collapsed:
            summary = collapsed[:280]
    return {
        "status": PROFILE_STATUS_BASIC,
        "title": title,
        "doc_category": category,
        "doc_subtype": None,
        "language": None,
        "primary_entities": [],
        "counterparties": [],
        "period_label": period_match.group(0).strip() if period_match else None,
        "effective_date": None,
        "document_number": None,
        "version_label": version_match.group(0).strip() if version_match else None,
        "summary": summary,
        "topics": [],
        "model": None,
    }


def _strip_json_fences(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    return text


def _clean_optional_str(value: Any, *, max_len: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    if not cleaned:
        return None
    return cleaned[:max_len]


def _parse_llm_profile(raw_content: str, *, fallback_title: str, model: str) -> dict[str, Any]:
    parsed = json.loads(_strip_json_fences(raw_content))
    if not isinstance(parsed, dict):
        raise ValueError("LLM profile response must be a JSON object")

    raw_category = parsed.get("doc_category")
    category = (
        raw_category.strip().lower()
        if isinstance(raw_category, str) and raw_category.strip().lower() in DOC_CATEGORY_SET
        else "other"
    )

    raw_date = _clean_optional_str(parsed.get("effective_date"), max_len=32)
    effective_date = raw_date if raw_date and _ISO_DATE_RE.match(raw_date) else None

    raw_lang = _clean_optional_str(parsed.get("language"), max_len=16)
    language = raw_lang.lower()[:16] if raw_lang else None

    topics_raw = parsed.get("topics")
    topics: list[str] = []
    if isinstance(topics_raw, list):
        seen_topics: set[str] = set()
        for item in topics_raw:
            if not isinstance(item, str):
                continue
            cleaned = re.sub(r"\s+", " ", item).strip().lower()[:60]
            if cleaned and cleaned not in seen_topics:
                seen_topics.add(cleaned)
                topics.append(cleaned)
            if len(topics) >= 8:
                break

    title = _clean_optional_str(parsed.get("title"), max_len=255) or fallback_title
    return {
        "status": PROFILE_STATUS_ENRICHED,
        "title": title,
        "doc_category": category,
        "doc_subtype": _clean_optional_str(parsed.get("doc_subtype"), max_len=120),
        "language": language,
        "primary_entities": normalize_entity_entries(parsed.get("primary_entities")),
        "counterparties": normalize_entity_entries(parsed.get("counterparties")),
        "period_label": _clean_optional_str(parsed.get("period_label"), max_len=64),
        "effective_date": effective_date,
        "document_number": _clean_optional_str(parsed.get("document_number"), max_len=120),
        "version_label": _clean_optional_str(parsed.get("version_label"), max_len=64),
        "summary": _clean_optional_str(parsed.get("summary"), max_len=600),
        "topics": topics,
        "model": model[:200],
    }


async def build_prose_digest(
    session: AsyncSession,
    *,
    document: Document,
    run_id: UUID,
) -> tuple[str, str | None]:
    """Assemble opening pages, document outline, and closing text for a prose run."""
    rows = (
        await session.execute(
            select(
                DocumentChunk.ordinal,
                DocumentChunk.text,
                DocumentChunk.token_count,
                DocumentChunk.section_path,
            )
            .where(DocumentChunk.processing_run_id == run_id)
            .order_by(DocumentChunk.ordinal)
        )
    ).all()

    opening_parts: list[str] = []
    opening_tokens = 0
    opening_chars = 0
    for _, text, token_count, _ in rows:
        if not text or not text.strip():
            continue
        if opening_parts and (
            opening_tokens >= _MAX_OPENING_TOKENS or opening_chars >= _MAX_OPENING_CHARS
        ):
            break
        remaining = _MAX_OPENING_CHARS - opening_chars
        snippet = text.strip()[:remaining]
        opening_parts.append(snippet)
        opening_tokens += max(int(token_count or 0), 1)
        opening_chars += len(snippet)

    headings: list[str] = []
    seen_headings: set[str] = set()
    for _, _, _, section_path in rows:
        if not isinstance(section_path, list) or not section_path:
            continue
        joined = " > ".join(str(part).strip() for part in section_path if str(part).strip())
        if joined and joined not in seen_headings:
            seen_headings.add(joined)
            headings.append(joined)
        if len(headings) >= _MAX_HEADINGS:
            break

    closing_text = ""
    if len(rows) > len(opening_parts):
        for _, text, _, _ in reversed(rows):
            if text and text.strip():
                closing_text = text.strip()[:_MAX_CLOSING_CHARS]
                break

    sections: list[str] = [
        f"Filename: {document.original_filename}",
        f"Media type: {document.detected_media_type}",
    ]
    if headings:
        sections.append("Outline headings:\n" + "\n".join(f"- {h}" for h in headings))
    if opening_parts:
        sections.append("Opening content:\n" + "\n\n".join(opening_parts))
    if closing_text:
        sections.append("Closing content:\n" + closing_text)

    first_snippet = opening_parts[0] if opening_parts else None
    return "\n\n".join(sections), first_snippet


async def build_tabular_digest(
    session: AsyncSession,
    *,
    document: Document,
    run: ProcessingRun,
) -> tuple[str, str | None]:
    """Assemble sheet catalogue and sample rows for a tabular run."""
    catalogue = run.csv_columns if isinstance(run.csv_columns, list) else []
    sections: list[str] = [
        f"Filename: {document.original_filename}",
        f"Media type: {document.detected_media_type}",
        f"Total rows: {run.row_count}",
    ]
    summary_parts: list[str] = []
    for entry in catalogue[:_MAX_TABULAR_SHEETS]:
        if not isinstance(entry, dict):
            continue
        sheet = str(entry.get("sheet") or "")
        raw_columns = entry.get("columns")
        columns = raw_columns if isinstance(raw_columns, list) else []
        row_count = entry.get("rows", 0)
        sheet_label = sheet or "(default sheet)"
        col_list = ", ".join(str(c) for c in columns[:30])
        sections.append(f"Sheet: {sheet_label} ({row_count} rows)\nColumns: {col_list}")
        summary_parts.append(f"{sheet_label}: {col_list}" if sheet else col_list)

        sample_rows = (
            await session.scalars(
                select(DocumentRow.data)
                .where(
                    DocumentRow.processing_run_id == run.id,
                    DocumentRow.sheet == sheet,
                )
                .order_by(DocumentRow.row_number)
                .limit(_MAX_TABULAR_SAMPLE_ROWS)
            )
        ).all()
        if sample_rows:
            formatted_rows = [json.dumps(row, ensure_ascii=False)[:400] for row in sample_rows]
            sections.append("Sample rows:\n" + "\n".join(formatted_rows))

    snippet = f"Tabular dataset with columns: {'; '.join(summary_parts)}" if summary_parts else None
    return "\n\n".join(sections), snippet


async def load_workspace_entity_vocabulary(
    session: AsyncSession,
    *,
    document_id: UUID,
    limit: int = _MAX_WORKSPACE_ENTITIES,
) -> list[str]:
    """Return top canonical entity names already used in this document's workspaces."""
    workspace_ids = list(
        (
            await session.scalars(
                select(DocumentWorkspace.workspace_id).where(
                    DocumentWorkspace.document_id == document_id,
                    DocumentWorkspace.deleted_at.is_(None),
                )
            )
        ).all()
    )
    if not workspace_ids:
        return []
    other_profiles = (
        await session.scalars(
            select(DocumentProfile.primary_entities)
            .join(
                DocumentWorkspace,
                DocumentWorkspace.document_id == DocumentProfile.document_id,
            )
            .where(
                DocumentWorkspace.workspace_id.in_(workspace_ids),
                DocumentWorkspace.deleted_at.is_(None),
                DocumentProfile.document_id != document_id,
            )
            .limit(200)
        )
    ).all()

    counts: Counter[str] = Counter()
    preferred_display: dict[str, str] = {}
    for entities in other_profiles:
        if not isinstance(entities, list):
            continue
        for entry in entities:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            normalized = entry.get("normalized")
            if isinstance(name, str) and isinstance(normalized, str) and normalized:
                counts[normalized] += 1
                preferred_display.setdefault(normalized, name)
    return [preferred_display[norm] for norm, _ in counts.most_common(limit)]


async def enrich_document(
    session: AsyncSession,
    *,
    document_id: UUID,
    run_id: UUID | None = None,
    commit: bool = True,
    provider_factory: Callable[[AgentModelConfig, str], OpenAICompatibleChatProvider] | None = None,
) -> DocumentProfile | None:
    """Build and persist the canonical ``DocumentProfile`` for one ready document.

    Uses the singleton ``AgentModelSetting`` when configured, falling back to a
    deterministic ``basic`` profile if unconfigured or on any model error.
    """
    document = await session.get(Document, document_id)
    if document is None or document.deleted_at is not None:
        return None

    target_run_id = run_id or document.active_processing_run_id
    if target_run_id is None:
        return None
    run = await session.get(ProcessingRun, target_run_id)
    if run is None:
        return None

    is_tabular = run.row_count > 0 and run.chunk_count == 0
    if is_tabular:
        digest, opening_snippet = await build_tabular_digest(session, document=document, run=run)
    else:
        digest, opening_snippet = await build_prose_digest(
            session, document=document, run_id=target_run_id
        )

    fallback_title = _clean_filename_title(document.original_filename)
    fields = _build_basic_profile_fields(
        filename=document.original_filename,
        digest=digest,
        is_tabular=is_tabular,
        opening_snippet=opening_snippet,
    )

    config = await load_agent_model_config(session)
    if config is not None:
        vocabulary = await load_workspace_entity_vocabulary(session, document_id=document_id)
        vocab_block = (
            "Known workspace entities (prefer exact spelling if referring to the same entity): "
            + ", ".join(vocabulary)
            if vocabulary
            else ""
        )
        user_prompt = f"{vocab_block}\n\n{digest}".strip()
        if provider_factory is not None:
            provider = provider_factory(config, fresh_session_id())
        else:
            provider = provider_for(config, session_id=fresh_session_id())
        try:
            turn = await provider.complete(
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                tools=[],
                temperature=0.0,
            )
            if turn.content:
                fields = _parse_llm_profile(
                    turn.content,
                    fallback_title=fallback_title,
                    model=config.model,
                )
            else:
                logger.warning(
                    "Document enrichment returned empty content; falling back to basic profile",
                    extra={"document_id": str(document_id)},
                )
        except (ChatProviderError, ValueError, KeyError, TypeError) as exc:
            logger.warning(
                "Document enrichment failed; falling back to basic profile: %s",
                exc,
                extra={"document_id": str(document_id)},
            )
        except Exception:
            logger.exception(
                "Unexpected error during document enrichment; falling back to basic profile",
                extra={"document_id": str(document_id)},
            )
        finally:
            await provider.aclose()

    search_text = build_search_text(
        title=fields["title"],
        doc_category=fields["doc_category"],
        doc_subtype=fields["doc_subtype"],
        primary_entities=fields["primary_entities"],
        counterparties=fields["counterparties"],
        period_label=fields["period_label"],
        effective_date=fields["effective_date"],
        document_number=fields["document_number"],
        version_label=fields["version_label"],
        topics=fields["topics"],
        summary=fields["summary"],
    )

    profile = await session.get(DocumentProfile, document_id)
    now = datetime.now(UTC)
    if profile is None:
        profile = DocumentProfile(
            document_id=document_id,
            processing_run_id=target_run_id,
            status=fields["status"],
            title=fields["title"],
            doc_category=fields["doc_category"],
            doc_subtype=fields["doc_subtype"],
            language=fields["language"],
            primary_entities=fields["primary_entities"],
            counterparties=fields["counterparties"],
            period_label=fields["period_label"],
            effective_date=fields["effective_date"],
            document_number=fields["document_number"],
            version_label=fields["version_label"],
            summary=fields["summary"],
            topics=fields["topics"],
            search_text=search_text,
            model=fields["model"],
            updated_at=now,
        )
        session.add(profile)
    else:
        profile.processing_run_id = target_run_id
        profile.status = fields["status"]
        profile.title = fields["title"]
        profile.doc_category = fields["doc_category"]
        profile.doc_subtype = fields["doc_subtype"]
        profile.language = fields["language"]
        profile.primary_entities = fields["primary_entities"]
        profile.counterparties = fields["counterparties"]
        profile.period_label = fields["period_label"]
        profile.effective_date = fields["effective_date"]
        profile.document_number = fields["document_number"]
        profile.version_label = fields["version_label"]
        profile.summary = fields["summary"]
        profile.topics = fields["topics"]
        profile.search_text = search_text
        profile.model = fields["model"]
        profile.updated_at = now

    if commit:
        await session.commit()
    else:
        await session.flush()
    return profile
