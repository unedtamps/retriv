"""Tests for automatic document intelligence extraction and heuristic fallback."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.chat import OpenAICompatibleChatProvider
from app.agents.model_settings import AgentModelConfig
from app.db.models import (
    JOB_EXTRACT,
    PROFILE_STATUS_BASIC,
    PROFILE_STATUS_ENRICHED,
    RUN_READY,
    AgentModelSetting,
    Document,
    DocumentChunk,
    DocumentRow,
    DocumentWorkspace,
    ProcessingRun,
)
from app.documents.intelligence import (
    build_search_text,
    enrich_document,
    normalize_entity_name,
)
from tests.conftest import SeededEntities


def test_normalize_entity_name_strips_legal_wrappers() -> None:
    assert normalize_entity_name("Apple Inc.") == "apple"
    assert normalize_entity_name("PT Bank Central Asia Tbk.") == "bank central asia"
    assert normalize_entity_name("CV Maju Jaya") == "maju jaya"
    assert normalize_entity_name("Acme Corp., Ltd.") == "acme"
    assert normalize_entity_name("PT") == "pt"


def test_build_search_text_flattens_entities_and_aliases() -> None:
    text = build_search_text(
        title="Form 10-K Annual Report",
        doc_category="financial_report",
        doc_subtype="Form 10-K",
        primary_entities=[
            {"name": "Apple Inc.", "normalized": "apple", "type": "company", "aliases": ["AAPL"]}
        ],
        counterparties=[],
        period_label="FY2025",
        effective_date="2025-09-27",
        document_number=None,
        version_label="As Filed",
        topics=["revenue", "10-k"],
        summary="Annual financial report for Apple Inc.",
    )
    assert "apple inc." in text
    assert "apple" in text
    assert "aapl" in text
    assert "fy2025" in text
    assert "form 10-k" in text


async def _seed_prose_document(
    session: AsyncSession,
    *,
    workspace_id: Any,
    uploader_id: Any,
    filename: str,
    chunks: list[tuple[str, list[str]]],
) -> tuple[Document, ProcessingRun]:
    doc_id = uuid4()
    run_id = uuid4()
    doc = Document(
        id=doc_id,
        origin_workspace_id=workspace_id,
        original_filename=filename,
        detected_media_type="application/pdf",
        size_bytes=1024,
        sha256=uuid4().hex + uuid4().hex,
        bucket="raw",
        object_key=f"raw/{doc_id}",
        created_by_user_id=uploader_id,
    )
    run = ProcessingRun(
        id=run_id,
        document_id=doc_id,
        origin_workspace_id=workspace_id,
        job_kind=JOB_EXTRACT,
        pipeline_version="v1",
        status=RUN_READY,
        chunk_count=len(chunks),
        embedded_chunk_count=len(chunks),
    )
    link = DocumentWorkspace(
        workspace_id=workspace_id,
        document_id=doc_id,
        filename=filename,
        created_by_user_id=uploader_id,
    )
    session.add_all([doc, run, link])
    await session.flush()
    doc.active_processing_run_id = run_id
    for ordinal, (text, section_path) in enumerate(chunks):
        session.add(
            DocumentChunk(
                origin_workspace_id=workspace_id,
                document_id=doc_id,
                processing_run_id=run_id,
                ordinal=ordinal,
                text=text,
                token_count=max(len(text.split()), 1),
                section_path=section_path,
                section_key=" > ".join(section_path),
                page_numbers=[ordinal + 1],
                content_hash=uuid4().hex + uuid4().hex,
            )
        )
    await session.commit()
    return doc, run


async def test_enrich_document_falls_back_to_basic_when_unconfigured(
    db_session_factory: async_sessionmaker[AsyncSession],
    entities: SeededEntities,
) -> None:
    async with db_session_factory() as session:
        doc, run = await _seed_prose_document(
            session,
            workspace_id=entities.finance.id,
            uploader_id=entities.user.id,
            filename="10-K-2025-As-Filed.pdf",
            chunks=[
                (
                    "UNITED STATES SECURITIES AND EXCHANGE COMMISSION Form 10-K Apple Inc.",
                    ["Cover"],
                ),
                ("Revenue for FY2025 was $391B.", ["Item 7", "Revenue"]),
            ],
        )
        profile = await enrich_document(session, document_id=doc.id, run_id=run.id)
        assert profile is not None
        assert profile.status == PROFILE_STATUS_BASIC
        assert profile.doc_category == "financial_report"
        assert profile.period_label == "2025"
        assert profile.model is None
        assert "financial report" in profile.search_text


async def test_enrich_document_uses_agent_model_setting_and_parses_json(
    db_session_factory: async_sessionmaker[AsyncSession],
    entities: SeededEntities,
) -> None:
    captured_prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured_prompts.append(body["messages"][1]["content"])
        payload = {
            "title": "Apple Inc. Form 10-K Annual Report",
            "doc_category": "financial_report",
            "doc_subtype": "Form 10-K",
            "language": "en",
            "primary_entities": [
                {"name": "Apple Inc.", "type": "company", "aliases": ["Apple", "AAPL"]}
            ],
            "counterparties": [],
            "period_label": "FY2025",
            "effective_date": "2025-09-27",
            "document_number": "001-36743",
            "version_label": "As Filed",
            "summary": (
                "Annual Form 10-K financial filing for Apple Inc. covering fiscal year 2025."
            ),
            "topics": ["10-k", "annual report", "revenue"],
        }
        return httpx.Response(
            200,
            json={
                "model": "test-chat-model",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": f"```json\n{json.dumps(payload)}\n```",
                        }
                    }
                ],
            },
        )

    def make_provider(config: AgentModelConfig, session_id: str) -> OpenAICompatibleChatProvider:
        return OpenAICompatibleChatProvider(
            base_url=config.base_url,
            api_key=config.api_key,
            model=config.model,
            session_id=session_id,
            transport=httpx.MockTransport(handler),
        )

    async with db_session_factory() as session:
        session.add(
            AgentModelSetting(
                id=1,
                provider="custom",
                base_url="https://llm.example.test/v1",
                api_key="sk-test",
                model="test-chat-model",
            )
        )
        await session.commit()

        doc, run = await _seed_prose_document(
            session,
            workspace_id=entities.finance.id,
            uploader_id=entities.user.id,
            filename="10-K-2025-As-Filed.pdf",
            chunks=[
                (
                    "UNITED STATES SECURITIES AND EXCHANGE COMMISSION FORM 10-K Apple Inc.",
                    ["Form 10-K"],
                ),
                ("Consolidated Statements of Operations.", ["Item 8"]),
            ],
        )

        profile = await enrich_document(
            session,
            document_id=doc.id,
            run_id=run.id,
            provider_factory=make_provider,
        )
        assert profile is not None
        assert profile.status == PROFILE_STATUS_ENRICHED
        assert profile.title == "Apple Inc. Form 10-K Annual Report"
        assert profile.doc_category == "financial_report"
        assert profile.doc_subtype == "Form 10-K"
        assert profile.period_label == "FY2025"
        assert profile.effective_date == "2025-09-27"
        assert profile.primary_entities[0]["normalized"] == "apple"
        assert "aapl" in profile.search_text
        assert "apple" in profile.search_text
        assert profile.model == "test-chat-model"
        assert captured_prompts and "10-K-2025-As-Filed.pdf" in captured_prompts[0]


async def test_enrich_document_falls_back_on_provider_error(
    db_session_factory: async_sessionmaker[AsyncSession],
    entities: SeededEntities,
) -> None:
    def failing_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "overloaded"}})

    def make_provider(config: AgentModelConfig, session_id: str) -> OpenAICompatibleChatProvider:
        return OpenAICompatibleChatProvider(
            base_url=config.base_url,
            api_key=config.api_key,
            model=config.model,
            session_id=session_id,
            transport=httpx.MockTransport(failing_handler),
        )

    async with db_session_factory() as session:
        session.add(
            AgentModelSetting(
                id=1,
                provider="custom",
                base_url="https://llm.example.test/v1",
                api_key="sk-test",
                model="test-chat-model",
            )
        )
        await session.commit()

        doc, run = await _seed_prose_document(
            session,
            workspace_id=entities.finance.id,
            uploader_id=entities.user.id,
            filename="HR_Leave_Policy_v2.pdf",
            chunks=[("Standard Operating Procedure for employee annual leave.", ["Leave Policy"])],
        )

        profile = await enrich_document(
            session,
            document_id=doc.id,
            run_id=run.id,
            provider_factory=make_provider,
        )
        assert profile is not None
        assert profile.status == PROFILE_STATUS_BASIC
        assert profile.doc_category == "policy_sop"
        assert profile.version_label == "v2"


async def test_enrich_tabular_document_builds_basic_dataset_profile(
    db_session_factory: async_sessionmaker[AsyncSession],
    entities: SeededEntities,
) -> None:
    async with db_session_factory() as session:
        doc_id = uuid4()
        run_id = uuid4()
        doc = Document(
            id=doc_id,
            origin_workspace_id=entities.finance.id,
            original_filename="sales_2025_q3.csv",
            detected_media_type="text/csv",
            size_bytes=512,
            sha256=uuid4().hex + uuid4().hex,
            bucket="raw",
            object_key=f"raw/{doc_id}",
            created_by_user_id=entities.user.id,
        )
        run = ProcessingRun(
            id=run_id,
            document_id=doc_id,
            origin_workspace_id=entities.finance.id,
            job_kind=JOB_EXTRACT,
            pipeline_version="v1",
            status=RUN_READY,
            chunk_count=0,
            row_count=2,
            csv_columns=[{"sheet": "", "columns": ["region", "revenue"], "rows": 2}],
        )
        link = DocumentWorkspace(
            workspace_id=entities.finance.id,
            document_id=doc_id,
            filename="sales_2025_q3.csv",
            created_by_user_id=entities.user.id,
        )
        session.add_all([doc, run, link])
        await session.flush()
        doc.active_processing_run_id = run_id
        session.add(
            DocumentRow(
                origin_workspace_id=entities.finance.id,
                document_id=doc_id,
                processing_run_id=run_id,
                sheet="",
                row_number=1,
                data={"region": "APAC", "revenue": "120000"},
            )
        )
        await session.commit()

        profile = await enrich_document(session, document_id=doc.id, run_id=run.id)
        assert profile is not None
        assert profile.doc_category == "tabular_dataset"
        assert "region" in (profile.summary or "")
