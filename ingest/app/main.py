from collections.abc import Awaitable, Callable
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from app.api.routes import documents, events, health, processing_runs, search
from app.core.config import Settings, get_settings
from app.core.errors import AppError


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        openapi_url=None if settings.environment == "production" else "/openapi.json",
        docs_url=None if settings.environment == "production" else "/docs",
        redoc_url=None,
    )
    application.include_router(health.router)
    application.include_router(events.router)
    application.include_router(documents.router)
    application.include_router(processing_runs.router)
    application.include_router(search.router)

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

    return application


app = create_app()
