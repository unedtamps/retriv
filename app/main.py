import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.responses import Response

from app.agent.harness import AnswerHarness
from app.agent.retrieval import ContextRetriever
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.db.session import engine, session_factory
from app.http.middleware import UploadSizeLimitMiddleware
from app.retrieval.sql import CatalogueReader, RowQuery, RowQueryError
from app.routes import (
    admin,
    agent,
    auth,
    conversations,
    documents,
    health,
    processing_runs,
    workspaces,
)

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        # The retriever and harness are process-wide: the Qdrant and
        # embedding clients hold connection pools, and building them per
        # request would pay for a handshake on every question.
        retriever = ContextRetriever(settings, engine)
        application.state.context_retriever = retriever
        # A reader DSN is what enables row queries. With none configured the
        # tool is simply absent from the agent, which is the safe default: an
        # unset DSN cannot be misread as "no restriction".
        rows: RowQuery | None = None
        if settings.sql_database_url:
            rows = RowQuery.from_url(
                settings.sql_database_url,
                statement_timeout_ms=settings.sql_statement_timeout_ms,
                max_result_rows=settings.sql_max_result_rows,
            )
            try:
                await rows.verify_reader()
            except RowQueryError as exc:
                await rows.aclose()
                raise RuntimeError(
                    "RETRIV_SQL_DATABASE_URL is configured but unusable"
                ) from exc
        application.state.row_query = rows
        # Reading through the app's own session lets a deployment without a
        # reader DSN still tell the model a CSV exists, rather than have it
        # report the data as missing when only the tool is off. The names are
        # not privileged: the document list already returns them.
        catalogue_reader = CatalogueReader(session_factory)
        harness = AnswerHarness(
            settings, retriever, rows=rows, catalogue_reader=catalogue_reader
        )
        application.state.answer_harness = harness
        try:
            yield
        finally:
            await harness.aclose()
            await retriever.aclose()
            if rows is not None:
                await rows.aclose()

    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    if settings is not None:
        application.dependency_overrides[get_settings] = lambda: settings
    application.add_middleware(
        UploadSizeLimitMiddleware,
        settings=settings,
    )
    if settings.cors_allowed_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
            expose_headers=["X-Request-ID"],
        )

    application.include_router(health.router)
    application.include_router(auth.router)
    application.include_router(admin.router)
    application.include_router(workspaces.router)
    application.include_router(documents.router)
    application.include_router(agent.router)
    application.include_router(conversations.router)
    application.include_router(processing_runs.router)

    @application.middleware("http")
    async def add_request_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = str(uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @application.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            headers=exc.headers,
            content={
                "type": f"/problems/{exc.code}",
                "title": exc.title,
                "status": exc.status_code,
                "detail": exc.detail,
                "code": exc.code,
                "instance": request.url.path,
            },
            media_type="application/problem+json",
        )

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = [
            {
                "field": ".".join(str(part) for part in error["loc"]),
                "message": error["msg"],
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "type": "/problems/request-validation",
                "title": "Request validation failed",
                "status": 422,
                "detail": "One or more request fields are invalid.",
                "code": "request_validation_failed",
                "instance": request.url.path,
                "errors": errors,
            },
            media_type="application/problem+json",
        )

    return application


app = create_app()
