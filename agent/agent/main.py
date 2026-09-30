import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import create_async_engine

from agent.config import Settings, get_settings
from agent.context_scope import InvalidContextScope, verify_context_scope
from agent.database import database_connect_args
from agent.dependencies import AnswerHarnessDependency
from agent.harness import AnswerGenerationUnavailable, AnswerHarness
from agent.retrieval import ContextRetriever, OutOfScopeSearchResult, RetrievalUnavailable
from agent.schemas import AnswerRequest, AnswerResponse

bearer = HTTPBearer(auto_error=False)


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        runtime = settings or get_settings()
        engine = create_async_engine(
            runtime.database_url.get_secret_value(),
            pool_pre_ping=True,
            connect_args=database_connect_args(runtime),
        )
        retriever = ContextRetriever(runtime, engine)
        application.state.context_retriever = retriever
        harness = AnswerHarness(runtime, retriever)
        application.state.answer_harness = harness
        try:
            yield
        finally:
            await harness.aclose()
            await retriever.aclose()

    application = FastAPI(
        title="Retriv Agent",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    if settings is not None:
        application.dependency_overrides[get_settings] = lambda: settings

    @application.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/v1/answer", response_model=AnswerResponse)
    async def answer(
        request: AnswerRequest,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None, Depends(bearer)
        ],
        runtime: Annotated[Settings, Depends(get_settings)],
        harness: AnswerHarnessDependency,
    ) -> AnswerResponse:
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not hmac.compare_digest(
                credentials.credentials, runtime.api_token.get_secret_value()
            )
        ):
            raise HTTPException(
                status_code=401,
                detail="Valid Retriv service credentials are required.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        try:
            scope = verify_context_scope(request.scope_token, runtime)
        except InvalidContextScope as exc:
            raise HTTPException(
                status_code=401,
                detail="The delegated workspace scope is invalid or expired.",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        if any(
            workspace_id not in scope.workspace_ids
            for message in request.history
            for workspace_id in message.source_workspace_ids
        ):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Conversation history contains a source outside the delegated workspace scope."
                ),
            )

        try:
            result = await harness.answer(
                request.question,
                scope.workspace_ids,
                request.history,
            )
        except OutOfScopeSearchResult as exc:
            raise HTTPException(
                status_code=403,
                detail="A retrieved context result was outside the delegated workspace scope.",
            ) from exc
        except RetrievalUnavailable as exc:
            raise HTTPException(
                status_code=502,
                detail="Scoped context retrieval failed validation.",
            ) from exc
        except AnswerGenerationUnavailable as exc:
            raise HTTPException(
                status_code=502,
                detail="The configured chat model could not complete the agent run.",
            ) from exc
        if any(source.workspace_id not in scope.workspace_ids for source in result.sources):
            raise HTTPException(
                status_code=403,
                detail="A retrieved context result was outside the delegated workspace scope.",
            )
        return result

    return application


app = create_app()
