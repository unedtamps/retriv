"""An OpenAI-compatible chat client with tool calling, for the harness.

The same discipline as the vision provider: a per-request timeout, an error
taxonomy that separates "try again" from "tell the user", and a check of
which model actually answered -- a gateway that silently routes a request to
a different model is caught here instead of being trusted in every turn.

The harness loop calls ``complete`` or ``stream_turn`` with the
conversation so far and the tool catalogue; the answer is text, tool calls,
or both. ``stream_turn`` is the one the dashboard path uses: the same
request with ``stream: true``, whose delta frames the harness forwards to
the browser as they arrive.
"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.vision.openai_compatible import chat_completions_url


class ChatProviderError(RuntimeError):
    """A failure talking to the chat model, marked retryable or not."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class ToolCall:
    """One tool the model asked for, arguments already parsed."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ChatTurn:
    """One assistant turn: prose, tool calls, or both."""

    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


class OpenAICompatibleChatProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 180.0,
        session_header: str | None = None,
        session_id: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = chat_completions_url(base_url)
        self._model = model
        headers = {"Authorization": f"Bearer {api_key}"}
        if session_header and session_id:
            headers[session_header] = session_id
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=timeout_seconds,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ChatTurn:
        body: dict[str, Any] = {"model": self._model, "messages": messages}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        parsed = await self._post(body)
        return self._read_turn(parsed)

    async def stream_turn(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AsyncIterator[dict[str, Any]]:
        """One model turn over a streaming response.

        Yields ``{"kind": "delta", "text": chunk}`` as prose tokens arrive,
        then exactly one ``{"kind": "turn", "turn": ChatTurn}`` carrying the
        fully-assembled turn -- content and parsed tool calls together -- so
        the caller runs the same logic it runs after ``complete``. Tool call
        fragments arrive spread across many frames keyed by index; their
        arguments string is only valid JSON once the stream has finished,
        which is why nothing is parsed before the final frame.
        """
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        async def frames() -> AsyncIterator[dict[str, Any]]:
            try:
                async with self._client.stream("POST", self._url, json=body) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        raise self._error_for_status(response)
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[len("data:"):].strip()
                        if not payload or payload == "[DONE]":
                            continue
                        try:
                            yield json.loads(payload)
                        except ValueError as exc:
                            raise ChatProviderError(
                                "The model endpoint streamed a frame that is not JSON",
                                retryable=True,
                            ) from exc
            except httpx.TimeoutException as exc:
                raise ChatProviderError(
                    "The model endpoint timed out", retryable=True
                ) from exc
            except httpx.HTTPError as exc:
                raise ChatProviderError(
                    f"The model endpoint could not be reached: {exc}", retryable=True
                ) from exc

        content_parts: list[str] = []
        fragments: dict[int, dict[str, Any]] = {}
        fragment_order: list[int] = []
        answered_by: str | None = None
        async for frame in frames():
            model = frame.get("model")
            if isinstance(model, str) and model.strip():
                answered_by = model.strip()
            choices = frame.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            delta = choices[0].get("delta")
            if not isinstance(delta, dict):
                continue
            chunk = delta.get("content")
            if isinstance(chunk, str) and chunk:
                content_parts.append(chunk)
                yield {"kind": "delta", "text": chunk}
            raw_calls = delta.get("tool_calls")
            if not isinstance(raw_calls, list):
                continue
            for raw in raw_calls:
                if not isinstance(raw, dict):
                    continue
                index = raw.get("index", 0)
                slot = fragments.get(index)
                if slot is None:
                    slot = {"id": None, "name": None, "arguments": ""}
                    fragments[index] = slot
                    fragment_order.append(index)
                identifier = raw.get("id")
                if isinstance(identifier, str) and identifier:
                    slot["id"] = identifier
                function = raw.get("function")
                if not isinstance(function, dict):
                    continue
                name = function.get("name")
                if isinstance(name, str) and name:
                    slot["name"] = name
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    slot["arguments"] += arguments

        if (
            answered_by is not None
            and answered_by.strip()
            and answered_by.strip().lower() != self._model.strip().lower()
        ):
            raise ChatProviderError(
                f"The endpoint answered with model '{answered_by.strip()}' "
                f"instead of '{self._model}'",
                retryable=False,
            )

        content = "".join(content_parts) or None
        calls: list[ToolCall] = []
        for index in fragment_order:
            slot = fragments[index]
            name = slot["name"]
            if not isinstance(name, str) or not name:
                continue
            try:
                arguments = (
                    json.loads(slot["arguments"]) if slot["arguments"].strip() else {}
                )
            except ValueError as exc:
                raise ChatProviderError(
                    f"The model returned malformed arguments for tool '{name}': {exc}",
                    retryable=False,
                ) from exc
            if not isinstance(arguments, dict):
                arguments = {}
            calls.append(
                ToolCall(
                    id=slot["id"] or f"call_{len(calls)}",
                    name=name,
                    arguments=arguments,
                )
            )
        yield {"kind": "turn", "turn": ChatTurn(content=content, tool_calls=calls)}

    async def test_connection(self) -> str:
        """One minimal completion; answers with the model that produced it.

        This is the check the admin sees: a configured endpoint that answers
        with a different model than the one named is a misconfiguration, and
        it is caught here rather than the first time a user asks a question.
        """
        parsed = await self._post(
            {
                "model": self._model,
                "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
                "max_tokens": 16,
            }
        )
        answered_by = parsed.get("model")
        if not isinstance(answered_by, str) or not answered_by.strip():
            raise ChatProviderError(
                "The model endpoint did not name the model that answered",
                retryable=False,
            )
        return answered_by.strip()

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(self._url, json=body)
        except httpx.TimeoutException as exc:
            raise ChatProviderError("The model endpoint timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ChatProviderError(
                f"The model endpoint could not be reached: {exc}", retryable=True
            ) from exc
        if response.status_code >= 400:
            raise self._error_for_status(response)
        try:
            parsed = response.json()
        except ValueError as exc:
            raise ChatProviderError(
                "The model endpoint returned a response that is not JSON",
                retryable=True,
            ) from exc
        return parsed

    def _read_turn(self, body: dict[str, Any]) -> ChatTurn:
        answered_by = body.get("model")
        if (
            isinstance(answered_by, str)
            and answered_by.strip()
            and answered_by.strip().lower() != self._model.strip().lower()
        ):
            raise ChatProviderError(
                f"The endpoint answered with model '{answered_by.strip()}' "
                f"instead of '{self._model}'",
                retryable=False,
            )
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ChatProviderError(
                "The model endpoint returned no choices", retryable=True
            )
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ChatProviderError(
                "The model endpoint returned an unexpected choice shape",
                retryable=True,
            )
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            content = None
        calls: list[ToolCall] = []
        raw_calls = message.get("tool_calls")
        if isinstance(raw_calls, list):
            for raw in raw_calls:
                if not isinstance(raw, dict):
                    continue
                function = raw.get("function")
                if not isinstance(function, dict):
                    continue
                name = function.get("name")
                if not isinstance(name, str) or not name:
                    continue
                arguments_raw = function.get("arguments")
                if isinstance(arguments_raw, str) and arguments_raw.strip():
                    try:
                        arguments = json.loads(arguments_raw)
                    except ValueError as exc:
                        raise ChatProviderError(
                            f"The model returned malformed arguments for "
                            f"tool '{name}': {exc}",
                            retryable=False,
                        ) from exc
                else:
                    arguments = {}
                if not isinstance(arguments, dict):
                    arguments = {}
                calls.append(
                    ToolCall(
                        id=raw.get("id") or f"call_{len(calls)}",
                        name=name,
                        arguments=arguments,
                    )
                )
        return ChatTurn(content=content, tool_calls=calls)

    def _error_for_status(self, response: httpx.Response) -> ChatProviderError:
        status = response.status_code
        detail = response.text[:200]
        if status in {401, 403}:
            return ChatProviderError(
                f"The model endpoint rejected the API key (status {status}): {detail}",
                retryable=False,
            )
        if status == 429:
            return ChatProviderError(
                f"The model endpoint is rate limiting requests (status {status})",
                retryable=True,
            )
        if status >= 500:
            return ChatProviderError(
                f"The model endpoint failed (status {status}): {detail}",
                retryable=True,
            )
        return ChatProviderError(
            f"The model endpoint rejected the request (status {status}): {detail}",
            retryable=False,
        )
