import asyncio

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings
from app.core.errors import AppError
from app.security.auth import decode_access_token


class UploadSizeLimitMiddleware:
    """Authenticate uploads and bound request bodies before multipart parsing."""

    _multipart_overhead_bytes = 1024 * 1024
    _auth_request_limit_bytes = 16 * 1024

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings
        self.max_upload_request_bytes = settings.max_upload_bytes + self._multipart_overhead_bytes
        self.upload_slots = asyncio.Semaphore(settings.max_concurrent_uploads)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request_kind = self._limited_request_kind(scope)
        if request_kind is None:
            await self.app(scope, receive, send)
            return

        if request_kind == "upload":
            auth_error = self._authenticate_upload(scope)
            if auth_error is not None:
                await self._send_authentication_error(scope, receive, send, auth_error)
                return
            limit = self.max_upload_request_bytes
        else:
            limit = self._auth_request_limit_bytes

        content_length = self._content_length(scope)
        if content_length is not None and content_length > limit:
            await self._send_size_rejection(scope, receive, send)
            return

        if request_kind == "upload":
            async with self.upload_slots:
                await self._call_with_stream_limit(scope, receive, send, limit)
        else:
            await self._call_with_stream_limit(scope, receive, send, limit)

    async def _call_with_stream_limit(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        limit: int,
    ) -> None:
        received_bytes = 0
        too_large = False
        timed_out = False
        response_messages: list[Message] = []
        deadline = (
            asyncio.get_running_loop().time() + self.settings.request_read_timeout_seconds
        )

        async def limited_receive() -> Message:
            nonlocal received_bytes, timed_out, too_large
            if too_large:
                return {"type": "http.disconnect"}
            try:
                remaining_seconds = deadline - asyncio.get_running_loop().time()
                if remaining_seconds <= 0:
                    raise TimeoutError
                message = await asyncio.wait_for(
                    receive(), timeout=remaining_seconds
                )
            except TimeoutError:
                timed_out = True
                return {"type": "http.disconnect"}
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > limit:
                    too_large = True
                    return {"type": "http.disconnect"}
            return message

        async def buffered_send(message: Message) -> None:
            response_messages.append(message)

        try:
            await self.app(scope, limited_receive, buffered_send)
        except Exception:
            if not too_large:
                raise

        if timed_out:
            await self._send_timeout_rejection(scope, receive, send)
            return
        if too_large:
            await self._send_size_rejection(scope, receive, send)
            return
        for message in response_messages:
            await send(message)

    def _authenticate_upload(self, scope: Scope) -> AppError | None:
        headers = dict(scope.get("headers", []))
        raw_authorization = headers.get(b"authorization", b"")
        try:
            authorization = raw_authorization.decode("ascii")
        except UnicodeDecodeError:
            authorization = ""
        scheme, separator, token = authorization.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not token:
            return self._authentication_error()
        try:
            decode_access_token(token, self.settings)
        except AppError as exc:
            return exc
        return None

    @staticmethod
    def _authentication_error() -> AppError:
        return AppError(
            status_code=401,
            code="invalid_credentials",
            title="Authentication failed",
            detail="The supplied credentials could not be validated.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @staticmethod
    def _limited_request_kind(scope: Scope) -> str | None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            return None
        path = str(scope.get("path", ""))
        if path == "/v1/auth/token":
            return "auth"
        segments = [segment for segment in path.split("/") if segment]
        if (
            len(segments) == 4
            and segments[0] == "v1"
            and segments[1] == "workspaces"
            and segments[3] == "documents"
        ):
            return "upload"
        return None

    @staticmethod
    def _content_length(scope: Scope) -> int | None:
        headers = dict(scope.get("headers", []))
        raw_value = headers.get(b"content-length")
        if raw_value is None:
            return None
        try:
            return int(raw_value)
        except ValueError:
            return None

    @staticmethod
    async def _send_authentication_error(
        scope: Scope, receive: Receive, send: Send, error: AppError
    ) -> None:
        response = JSONResponse(
            status_code=error.status_code,
            headers=error.headers,
            content={
                "type": f"/problems/{error.code}",
                "title": error.title,
                "status": error.status_code,
                "detail": error.detail,
                "code": error.code,
                "instance": str(scope.get("path", "")),
            },
            media_type="application/problem+json",
        )
        await response(scope, receive, send)

    @staticmethod
    async def _send_size_rejection(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content={
                "type": "/problems/file-too-large",
                "title": "File too large",
                "status": 413,
                "detail": "The request exceeds the configured size limit.",
                "code": "file_too_large",
                "instance": str(scope.get("path", "")),
            },
            media_type="application/problem+json",
        )
        await response(scope, receive, send)

    @staticmethod
    async def _send_timeout_rejection(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=408,
            content={
                "type": "/problems/request-timeout",
                "title": "Request timeout",
                "status": 408,
                "detail": "The request body was not received within the allowed time.",
                "code": "request_timeout",
                "instance": str(scope.get("path", "")),
            },
            media_type="application/problem+json",
        )
        await response(scope, receive, send)
