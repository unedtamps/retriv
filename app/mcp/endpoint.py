"""The HTTP gate in front of the MCP endpoint.

Mounted as ASGI middleware rather than as a route so that ``/mcp`` is decided
before anything else sees it: FastAPI's own routes are matched first anyway
(this middleware passes everything that is not ``/mcp`` straight through), and
rejecting an unauthenticated call here means no session is allocated for a
caller who cannot use it.

The middleware is a gate, not the authority. Each tool re-authenticates from
the headers of its own request, because revocation has to bite on the next
call and not when the session ends.
"""

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import AppError
from app.mcp.auth import user_from_authorization
from app.mcp.runtime import McpNotReadyError, McpRuntimeBox

_PROBLEM_MEDIA_TYPE = "application/problem+json"


class McpEndpointMiddleware:
    """Authenticate ``/mcp`` traffic and hand it to the MCP application."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        mcp_app: ASGIApp,
        box: McpRuntimeBox,
        path: str = "/mcp",
    ) -> None:
        self.app = app
        self.mcp_app = mcp_app
        self.box = box
        self._path = path
        self._paths = frozenset({path, f"{path}/"})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path") if scope["type"] == "http" else None
        if path not in self._paths:
            await self.app(scope, receive, send)
            return

        try:
            session_factory = self.box.runtime.session_factory
        except McpNotReadyError:
            await _send_problem(
                scope,
                receive,
                send,
                AppError(
                    status_code=503,
                    code="mcp_not_ready",
                    title="MCP server not ready",
                    detail="The MCP endpoint is not accepting requests yet.",
                ),
            )
            return

        try:
            async with session_factory() as session:
                await user_from_authorization(_authorization(scope), session)
        except AppError as exc:
            await _send_problem(scope, receive, send, exc)
            return

        # The MCP application is dispatched to directly rather than mounted:
        # a Mount at "/mcp" makes the router answer "/mcp" with a redirect to
        # "/mcp/", and an MCP client that does not follow redirects would see
        # a 307 instead of a session.
        if path != self._path:
            scope["path"] = self._path
        await self.mcp_app(scope, receive, send)


def _authorization(scope: Scope) -> str | None:
    for name, value in scope.get("headers", ()):
        if name == b"authorization":
            try:
                return value.decode("latin-1")
            except UnicodeDecodeError:
                return None
    return None


async def _send_problem(
    scope: Scope, receive: Receive, send: Send, error: AppError
) -> None:
    payload = {
        "type": f"/problems/{error.code}",
        "title": error.title,
        "status": error.status_code,
        "detail": error.detail,
        "code": error.code,
        "instance": str(scope.get("path", "")),
    }
    body = json.dumps(payload).encode("utf-8")
    extra_headers = (
        (name.encode("latin-1"), value.encode("latin-1"))
        for name, value in (error.headers or {}).items()
    )
    headers = [
        (b"content-type", _PROBLEM_MEDIA_TYPE.encode("ascii")),
        (b"content-length", str(len(body)).encode("ascii")),
        *extra_headers,
    ]
    start: Message = {
        "type": "http.response.start",
        "status": error.status_code,
        "headers": headers,
    }
    await send(start)
    await send({"type": "http.response.body", "body": body})
