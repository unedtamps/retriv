"""The contract between the pipeline and whatever describes a picture."""

from typing import Protocol

from app.extraction.base import ExtractedImage


class VisionProviderError(Exception):
    """A description could not be produced.

    ``retryable`` separates the two kinds of failure. A timeout, a rate limit
    and a gateway that is briefly down are worth another attempt; a rejected
    key or a request the endpoint refuses outright are not, and retrying them
    only delays the report that something is misconfigured.
    """

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


DESCRIPTION_PROMPT = """\
This image is a picture taken from a document. Describe it so that someone \
searching the document can find it by what it depicts.

Say what kind of picture it is: a chart, a diagram, a photograph, a map, a \
screenshot, an illustration, and so on. For a chart, name the axes, the \
series and the values you can read. For a diagram, name the parts and the \
relationship between them. For a photograph, say what is in it. Read out any \
text, labels, numbers, units and legends that are visible.

Answer in plain prose, at most three sentences. Report only what you can \
actually see: if something is illegible, say that it is illegible rather than \
guessing what it says.
"""


def build_context(section_path: list[str], caption: str) -> str:
    """Tell the model where the picture sits, so it can describe it in context.

    A picture on its own is often ambiguous -- a bar chart of nothing in
    particular. The heading it sits under and the caption printed next to it
    say what the chart is *of*, and a description that knows that is worth
    several descriptions that do not.
    """
    parts: list[str] = []
    if section_path:
        heading = " > ".join(part for part in section_path if part.strip())
        if heading:
            parts.append(
                f'The picture appears in a document under the heading "{heading}".'
            )
    if caption.strip():
        parts.append(f"The caption printed next to it reads: {caption.strip()}")
    if not parts:
        return ""
    return "\n".join(parts) + "\n\n"


class VisionProvider(Protocol):
    """Describes one picture at a time."""

    async def describe(self, image: ExtractedImage, *, context: str = "") -> str: ...

    async def aclose(self) -> None: ...
