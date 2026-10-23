"""Describing a picture, and putting the description back in the chunk.

Two things are tested here that the rest of the pipeline depends on: that the
vision client asks the model the right question and refuses an answer that
came from a different model, and that a description lands on the line the
picture was on rather than somewhere else in the document.
"""

import json
from typing import Any

import httpx
import pytest

from app.extraction.base import ExtractedChunk, ExtractedImage
from app.extraction.images import (
    merge_descriptions,
    picture_marker,
    picture_refs,
)
from app.sections import encode_section_key
from app.vision.base import VisionProviderError
from app.vision.openai_compatible import (
    OpenAICompatibleVisionProvider,
    chat_completions_url,
    clean_description,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\nnot-really-a-png"


def an_image(ref: str = "#/pictures/0", caption: str = "") -> ExtractedImage:
    return ExtractedImage(
        ref=ref,
        page_number=3,
        media_type="image/png",
        data=PNG_BYTES,
        caption=caption,
        width=600,
        height=300,
    )


def a_chunk(text: str) -> ExtractedChunk:
    return ExtractedChunk(
        text=text,
        token_count=1,
        section_path=["3.2 Financial Performance"],
        section_key=encode_section_key(["3.2 Financial Performance"]),
        is_heading_only=False,
        page_numbers=[3],
        provenance={},
    )


def a_provider(
    handler: Any, **overrides: Any
) -> OpenAICompatibleVisionProvider:
    options: dict[str, Any] = {
        "base_url": "http://vision.test/v1/chat/completions",
        "api_key": "test-key",
        "model": "vendor/vision-model",
        "timeout_seconds": 1,
        "wait_base_seconds": 0.01,
        "wait_max_seconds": 0.01,
        "wait_timeout_seconds": 1,
        "transport": httpx.MockTransport(handler),
    }
    options.update(overrides)
    return OpenAICompatibleVisionProvider(**options)


def an_answer(content: str, model: str = "vendor/vision-model") -> dict[str, Any]:
    return {
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
    }


def an_answer_that_ran_out_of_budget() -> dict[str, Any]:
    """What a reasoning model returns when it thinks for the whole budget.

    The answer never starts, so there is nothing to read.
    """
    return {
        "model": "vendor/vision-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "length",
                "message": {"role": "assistant", "content": None, "reasoning": "..."},
            }
        ],
    }


async def test_the_client_sends_the_picture_and_returns_the_description() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=an_answer("A bar chart of net revenue."))

    provider = a_provider(handler)
    try:
        description = await provider.describe(an_image(), context="Under a heading.")
    finally:
        await provider.aclose()

    assert description == "A bar chart of net revenue."
    assert seen["url"] == "http://vision.test/v1/chat/completions"
    body = seen["body"]
    assert body["model"] == "vendor/vision-model"
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert "Under a heading." in content[0]["text"]
    assert "at most three sentences" in content[0]["text"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


async def test_an_answer_from_another_model_is_refused() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=an_answer("A bar chart.", model="other/model"))

    provider = a_provider(handler)
    try:
        with pytest.raises(VisionProviderError, match="answered with model") as error:
            await provider.describe(an_image())
    finally:
        await provider.aclose()

    # A different model answered, so the answer is not about this picture.
    # It is refused outright rather than retried: the endpoint is routing
    # requests somewhere else, and no amount of waiting fixes that.
    assert error.value.retryable is False
    assert calls == 1


async def test_a_transient_failure_is_retried() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": "busy"})
        return httpx.Response(200, json=an_answer("A pie chart of market share."))

    provider = a_provider(handler)
    try:
        description = await provider.describe(an_image())
    finally:
        await provider.aclose()

    assert description == "A pie chart of market share."
    assert calls == 2


async def test_a_rejected_key_is_not_retried() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": "bad key"})

    provider = a_provider(handler)
    try:
        with pytest.raises(VisionProviderError, match="rejected the API key") as error:
            await provider.describe(an_image())
    finally:
        await provider.aclose()

    assert error.value.retryable is False
    assert calls == 1


async def test_a_rate_limit_is_retried() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": "slow down"})
        return httpx.Response(200, json=an_answer("A photograph of a building."))

    provider = a_provider(handler)
    try:
        assert await provider.describe(an_image()) == "A photograph of a building."
    finally:
        await provider.aclose()

    assert calls == 2


async def test_a_description_longer_than_the_cap_is_truncated() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=an_answer("word " * 500))

    provider = a_provider(handler, max_description_chars=50)
    try:
        description = await provider.describe(an_image())
    finally:
        await provider.aclose()

    assert len(description) <= 50


async def test_an_empty_answer_is_refused() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=an_answer("   "))

    provider = a_provider(handler)
    try:
        with pytest.raises(VisionProviderError, match="no text"):
            await provider.describe(an_image())
    finally:
        await provider.aclose()


async def test_a_model_that_thinks_for_the_whole_budget_is_asked_again() -> None:
    """A reasoning model spends the budget thinking before it writes.

    When the budget runs out first the answer never starts, and the model
    was not finished describing the picture -- it was cut off. So the
    budget goes up and the same picture is asked again, rather than the
    document being failed over a picture the model was still working on.
    """
    budgets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        budgets.append(json.loads(request.content)["max_tokens"])
        if len(budgets) == 1:
            return httpx.Response(200, json=an_answer_that_ran_out_of_budget())
        return httpx.Response(200, json=an_answer("A bar chart of net revenue."))

    provider = a_provider(handler, max_tokens=1024)
    try:
        description = await provider.describe(an_image())
    finally:
        await provider.aclose()

    assert description == "A bar chart of net revenue."
    assert budgets == [1024, 2048]


async def test_a_model_that_never_finishes_is_reported() -> None:
    """When even the ceiling is not enough, the run is told so.

    The picture is not wrong and the model is not broken; the ceiling is
    simply lower than what this picture takes, and only the operator can
    raise it.
    """
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=an_answer_that_ran_out_of_budget())

    provider = a_provider(handler, max_tokens=1024, max_tokens_ceiling=2048)
    try:
        with pytest.raises(VisionProviderError, match="still thinking") as error:
            await provider.describe(an_image())
    finally:
        await provider.aclose()

    assert error.value.retryable is False


def test_the_markdown_around_an_answer_is_removed() -> None:
    assert (
        clean_description('The chart reads **"REVENUE"** and _2015_ was strong.')
        == 'The chart reads "REVENUE" and 2015 was strong.'
    )
    assert clean_description("```\nA chart.\n```") == "A chart."


def test_a_bare_api_root_gets_the_chat_endpoint() -> None:
    assert (
        chat_completions_url("https://gateway.test/v1")
        == "https://gateway.test/v1/chat/completions"
    )
    assert (
        chat_completions_url("https://gateway.test/v1/chat/completions")
        == "https://gateway.test/v1/chat/completions"
    )


def test_a_marker_is_replaced_by_the_description() -> None:
    chunk = a_chunk(
        "3.2 Financial Performance\nThe chart follows.\n"
        f"{picture_marker('#/pictures/0')}\nRevenue grew.\n"
    )
    merged = merge_descriptions(
        [chunk],
        {"#/pictures/0": "A bar chart of net revenue by segment."},
        tokenizer_name="cl100k_base",
        model="vendor/vision-model",
    )

    assert len(merged) == 1
    text = merged[0].text
    assert "[Image description: A bar chart of net revenue by segment.]" in text
    assert picture_refs(text) == []
    # The description lands on the line the picture was on, between the
    # sentence that introduces it and the sentence that follows it.
    lines = text.splitlines()
    assert lines[1] == "The chart follows."
    assert lines[2].startswith("[Image description:")
    assert lines[3] == "Revenue grew."


def test_a_picture_that_was_never_described_loses_its_marker() -> None:
    chunk = a_chunk(f"Before.\n{picture_marker('#/pictures/7')}\nAfter.")
    merged = merge_descriptions(
        [chunk],
        {},
        tokenizer_name="cl100k_base",
        model="vendor/vision-model",
    )

    assert picture_refs(merged[0].text) == []
    assert merged[0].text == "Before.\n[Image]\nAfter."


def test_a_chunk_of_nothing_but_a_picture_stays_readable() -> None:
    chunk = a_chunk(picture_marker("#/pictures/0"))
    merged = merge_descriptions(
        [chunk],
        {},
        tokenizer_name="cl100k_base",
        model="vendor/vision-model",
    )

    # An empty chunk is a permanent processing error, so the marker becomes
    # a word rather than nothing.
    assert merged[0].text.strip() == "[Image]"


def test_the_token_count_is_recounted_after_a_merge() -> None:
    chunk = a_chunk(f"Before.\n{picture_marker('#/pictures/0')}")
    merged = merge_descriptions(
        [chunk],
        {"#/pictures/0": "A bar chart of net revenue by segment."},
        tokenizer_name="cl100k_base",
        model="vendor/vision-model",
    )

    assert merged[0].token_count > chunk.token_count
    assert merged[0].provenance["images"] == [
        {"ref": "#/pictures/0", "described": True}
    ]
    assert merged[0].provenance["image_model"] == "vendor/vision-model"


def test_a_chunk_without_a_picture_is_left_alone() -> None:
    chunk = a_chunk("Just text.")
    merged = merge_descriptions(
        [chunk],
        {"#/pictures/0": "A bar chart."},
        tokenizer_name="cl100k_base",
        model="vendor/vision-model",
    )

    assert merged[0] is chunk


def test_a_bigger_budget_is_given_more_time_on_the_wire() -> None:
    """A client that gives up while the model is still thinking looks
    exactly like a model that failed."""
    provider = a_provider(
        lambda _: httpx.Response(200, json=an_answer("A chart.")),
        max_tokens=1024,
        timeout_seconds=10,
    )
    # A budget at or below the starting one gets the starting timeout, and a
    # bigger one gets proportionally more.
    assert provider._timeout_for(1024) == 10
    assert provider._timeout_for(256) == 10
    assert provider._timeout_for(2048) == 20
    assert provider._timeout_for(4096) == 40
