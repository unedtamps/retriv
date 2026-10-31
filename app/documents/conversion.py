"""Turning a slide deck into the format the pipeline already reads.

A presentation is not a document with paragraphs: a slide is a canvas, and its
charts and SmartArt are stored as recipes -- data plus a chart type -- rather
than as pictures. A parser that reads the XML sees the recipe, not the chart,
so the figure a slide exists to show would vanish. Rendering the deck instead
makes every one of those into pixels on a page, and the PDF path -- which
already handles layout, borderless tables, two-column flow and figure
description -- reads it without knowing where it came from.

The conversion happens in the worker, never in the API: the API image stays
small by carrying no office suite at all.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

CONVERSION_TIMEOUT_SECONDS = 180
"""Ceiling for one deck. A busy 200-slide deck on a cold cache is minutes on a
netbook and seconds here; anything past this is a deck that will not convert."""

_SOFFICE_CANDIDATES = ("soffice", "libreoffice")


class ConversionError(Exception):
    """The deck could not be rendered to PDF."""


class ConversionUnavailableError(ConversionError):
    """No office suite is installed in this image.

    Kept apart from a failed conversion: one is a deployment that forgot the
    dependency, the other is a document that will not convert either way. The
    retry policy differs -- and neither is worth retrying silently.
    """


def _find_soffice() -> str:
    for candidate in _SOFFICE_CANDIDATES:
        found = shutil.which(candidate)
        if found is not None:
            return found
    raise ConversionUnavailableError(
        "No LibreOffice binary is installed, so a presentation cannot be converted"
    )


def convert_to_pdf(source: Path, *, timeout_seconds: int = CONVERSION_TIMEOUT_SECONDS) -> Path:
    """Render one deck to a PDF beside it, and return the PDF's path.

    LibreOffice writes into ``--outdir`` using the input's stem, so the result
    is predictable without parsing its stdout. A private profile directory is
    passed through HOME and UserInstallation: without it the suite tries to
    write into the container's read-only home and fails on the first run.

    The caller owns the returned file and everything the conversion created;
    it lives in a temporary directory so a crash cannot leave a stray PDF in
    the workspace.
    """
    binary = _find_soffice()
    if not source.is_file():
        raise ConversionError(f"No such deck: {source}")
    if source.suffix.lower() != ".pptx":
        raise ConversionError(f"Only .pptx is converted, not {source.suffix}")

    with tempfile.TemporaryDirectory(prefix="deck-conversion-") as profile:
        output_dir = Path(tempfile.mkdtemp(prefix="deck-pdf-", dir=source.parent))
        environment = {
            "HOME": profile,
            "PATH": f"{Path(binary).parent}:/usr/local/bin:/usr/bin:/bin",
            "TMPDIR": profile,
        }
        command = [
            binary,
            "--headless",
            "--norestore",
            "--invisible",
            "--nolockcheck",
            "--nodefault",
            f"-env:UserInstallation=file://{profile}",
            "--convert-to",
            "pdf:impress_pdf_Export",
            "--outdir",
            str(output_dir),
            str(source),
        ]
        try:
            completed = subprocess.run(  # noqa: S603 - fixed binary, project-owned input
                command,
                env=environment,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            shutil.rmtree(output_dir, ignore_errors=True)
            raise ConversionError(
                f"The deck did not convert within {timeout_seconds}s"
            ) from exc

        produced = output_dir / f"{source.stem}.pdf"
        if completed.returncode != 0 or not produced.is_file() or produced.stat().st_size == 0:
            detail = (completed.stderr or b"").decode("utf-8", "replace").strip()
            shutil.rmtree(output_dir, ignore_errors=True)
            raise ConversionError(
                f"The deck could not be rendered to PDF (exit {completed.returncode})"
                + (f": {detail[:300]}" if detail else "")
            )
        return produced
