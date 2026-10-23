"""An OpenAI-compatible client for the multimodal model.

The endpoint is asked for a chat completion with one image attached, which is
the shape every OpenAI-compatible gateway accepts: the image goes in as a
``data:`` URL under an ``image_url`` part, so nothing is uploaded anywhere
except to the model that is going to describe it.

Three things this client is careful about:

*   **It checks which model answered.** A gateway that silently routes a
    request to a text-only model returns a plausible-looking answer about an
    image it never saw. The response carries the model that produced it, and
    a mismatch is refused rather than indexed.
*   **It tells the two kinds of failure apart.** A timeout or a 503 is worth
    another attempt; a rejected key is not, and retrying it only delays the
    report that something is misconfigured.
*   **It does not leak markdown.** The answer is text that will be embedded
    and read by another model, so the emphasis marks and code fences come
    off before it leaves here.
"""

import asyncio
import base64
import re
import time
from typing import Any

import httpx

from app.extraction.base import ExtractedImage
from app.vision.base import DESCRIPTION_PROMPT, VisionProviderError

# The endpoint may be given as a bare API root or as the full chat endpoint.
# Both are common, so both are accepted rather than one of them silently
# producing a 404.
_CHAT_SUFFIX = "/chat/completions"

_MARKDOWN_FENCE = re.compile(r"^```[a-zA-Z0-9]*\s*|\s*```$")
_EMPHASIS = re.compile(r"(\*\*|__|\*|_)(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_LEADING_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def chat_completions_url(base_url: str) -> str:
    """Return the chat endpoint to post to."""
    trimmed = base_url.rstrip("/")
    if trimmed.endswith(_CHAT_SUFFIX):
        return trimmed
    return f"{trimmed}{_CHAT_SUFFIX}"


def clean_description(text: str) -> str:
    """Strip the formatting a chat model puts around a plain answer."""
    cleaned = text.strip()
    cleaned = _MARKDOWN_FENCE.sub("", cleaned).strip()
    cleaned = _EMPHASIS.sub(r"\2", cleaned)
    cleaned = _LEADING_BULLET.sub("", cleaned)
    return re.sub(r"[ \t]+", " ", cleaned).strip()


class _BudgetExhausted(VisionProviderError):
    """The model was still thinking when the answer budget ran out.

    Separate from the other failures because it has a different remedy: the
    budget goes up, and the same picture is asked again. The model did not
    fail to describe the picture -- it was not finished describing it.
    """


class OpenAICompatibleVisionProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_tokens: int = 8192,
        max_tokens_ceiling: int = 131_072,
        max_description_chars: int = 1200,
        wait_base_seconds: float = 5.0,
        wait_max_seconds: float = 60.0,
        wait_timeout_seconds: float = 3600.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = chat_completions_url(base_url)
        self._model = model
        self._max_tokens = max_tokens
        self._max_tokens_ceiling = max_tokens_ceiling
        self._timeout_seconds = timeout_seconds
        self._max_description_chars = max_description_chars
        self._wait_base_seconds = wait_base_seconds
        self._wait_max_seconds = wait_max_seconds
        self._wait_timeout_seconds = wait_timeout_seconds
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
            transport=transport,
        )

    async def describe(self, image: ExtractedImage, *, context: str = "") -> str:
        """Describe one picture, retrying a transient failure.

        The answer budget is a ceiling on the model's whole answer, thinking
        included, and a reasoning model spends most of it thinking before it
        writes anything. The model stops on its own well below a generous
        ceiling, so the ceiling is not a limit on how long it may think --
        but when it is set below what the model wanted, the endpoint cuts it
        off mid-thought and hands back nothing. So when that happens the
        budget goes up and the same picture is asked again, rather than the
        document being failed over a picture the model was still working on.

        The retry budget for a failure that is not about the budget is the
        model's wait budget: the stage is allowed to sit and wait for a
        model that is slow to start, but not forever.
        """
        payload = self._build_payload(image, context=context)
        budget = self._max_tokens
        deadline = time.monotonic() + self._wait_timeout_seconds
        delay = self._wait_base_seconds
        while True:
            try:
                return await self._describe_once(payload, budget)
            except _BudgetExhausted:
                if budget >= self._max_tokens_ceiling or time.monotonic() > deadline:
                    raise VisionProviderError(
                        f"The vision model was still thinking after {budget} tokens; "
                        f"raise RETRIV_VLM_MAX_TOKENS",
                        retryable=False,
                    ) from None
                budget = min(budget * 2, self._max_tokens_ceiling)
            except VisionProviderError as exc:
                if not exc.retryable:
                    raise
                if time.monotonic() + delay > deadline:
                    raise VisionProviderError(
                        f"The vision endpoint did not answer within "
                        f"{self._wait_timeout_seconds:.0f}s: {exc}",
                        retryable=False,
                    ) from exc
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._wait_max_seconds)

    def _timeout_for(self, budget: int) -> float:
        """How long a request with this budget is given on the wire.

        A bigger budget means a longer possible generation, and a client
        that gives up while the model is still thinking looks exactly like
        a model that failed. So the timeout grows with the budget, and the
        processing timeout stays the backstop for the whole stage.
        """
        if self._max_tokens <= 0:
            return self._timeout_seconds
        return self._timeout_seconds * max(1.0, budget / self._max_tokens)

    async def _describe_once(self, payload: dict[str, Any], budget: int) -> str:
        body = dict(payload)
        body["max_tokens"] = budget
        try:
            response = await self._client.post(
                self._url, json=body, timeout=self._timeout_for(budget)
            )
        except httpx.TimeoutException as exc:
            raise VisionProviderError(
                "The vision endpoint timed out", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise VisionProviderError(
                f"The vision endpoint could not be reached: {exc}", retryable=True
            ) from exc

        if response.status_code >= 400:
            raise self._error_for_status(response)

        try:
            parsed = response.json()
        except ValueError as exc:
            raise VisionProviderError(
                "The vision endpoint returned a response that is not JSON",
                retryable=True,
            ) from exc
        return self._read_description(parsed, budget)

    def _error_for_status(self, response: httpx.Response) -> VisionProviderError:
        status = response.status_code
        detail = response.text[:200]
        if status in {401, 403}:
            return VisionProviderError(
                f"The vision endpoint rejected the API key (status {status}): {detail}",
                retryable=False,
            )
        if status == 429:
            return VisionProviderError(
                f"The vision endpoint is rate limiting this worker (status {status})",
                retryable=True,
            )
        if status >= 500:
            return VisionProviderError(
                f"The vision endpoint failed (status {status}): {detail}",
                retryable=True,
            )
        return VisionProviderError(
            f"The vision endpoint rejected the request (status {status}): {detail}",
            retryable=False,
        )

    def _read_description(self, body: Any, budget: int) -> str:
        if not isinstance(body, dict):
            raise VisionProviderError(
                "The vision endpoint returned an unexpected response", retryable=True
            )
        answered_by = body.get("model")
        if (
            isinstance(answered_by, str)
            and answered_by.strip()
            and answered_by.strip().lower() != self._model.strip().lower()
        ):
            # A different model answered, so the answer is about something
            # else -- or about nothing, if it could not see the image. Either
            # way it is not what was asked for, and it must not be indexed.
            raise VisionProviderError(
                f"The vision endpoint answered with model {answered_by!r} "
                f"but {self._model!r} was requested",
                retryable=False,
            )
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise VisionProviderError(
                "The vision endpoint returned no choices", retryable=True
            )
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message")
        if not isinstance(message, dict):
            raise VisionProviderError(
                "The vision endpoint returned no message", retryable=True
            )
        text = self._message_text(message.get("content"))
        if not text:
            # A model that reasons spends the budget on reasoning before it
            # writes anything. When the budget runs out first, the answer
            # never starts: the model was not finished, not wrong. That is
            # the caller's cue to give it more room and ask again.
            if choice.get("finish_reason") == "length":
                raise _BudgetExhausted(
                    f"The vision model used its whole answer budget "
                    f"({budget} tokens) without describing the "
                    f"picture",
                    retryable=False,
                )
            raise VisionProviderError(
                "The vision model returned no text for this picture", retryable=False
            )
        description = clean_description(text)
        if len(description) > self._max_description_chars:
            description = description[: self._max_description_chars].rstrip()
        return description

    @staticmethod
    def _message_text(content: Any) -> str:
        """Read the answer, whether it is a string or a list of parts."""
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif isinstance(part, str):
                    parts.append(part)
            return "\n".join(parts).strip()
        return ""

    def _build_payload(self, image: ExtractedImage, *, context: str) -> dict[str, Any]:
        encoded = base64.b64encode(image.data).decode("ascii")
        prompt = f"{context}{DESCRIPTION_PROMPT}" if context else DESCRIPTION_PROMPT
        return {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{image.media_type};base64,{encoded}"
                            },
                        },
                    ],
                }
            ],
            "max_tokens": self._max_tokens,
        }

    async def aclose(self) -> None:
        await self._client.aclose()
