import logging
from collections.abc import Awaitable, Callable
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.responses import Response

from app.api.routes import (
    admin,
    agent,
    auth,
    conversations,
    documents,
    health,
    processing_runs,
    workspaces,
)
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.http.middleware import UploadSizeLimitMiddleware

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url=None,
    )
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
