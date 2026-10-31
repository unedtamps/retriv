#!/usr/bin/env python
"""Measure what parallel extraction actually costs in RAM and CPU.

Runs N copies of the real extraction sandbox at the same time, one per
document, and samples each process while it works. The numbers this prints
are the ones the deployment cares about: peak resident memory per sandbox,
total resident memory across the batch, CPU-seconds burned, and how much
wall-clock a second or third parser actually buys.

The sandbox is spawned exactly as the worker spawns it -- same module, same
resource limits, same thread caps -- so the measurement reflects production
and not a convenient in-process shortcut. The child's stdout/stderr go to a
log file beside the output, because a sandbox that dies has its reason in
there.

Two kinds of run are worth comparing:

    inside the worker container  (docker exec ...)   <- the cgroup applies
    on the host                  (plain python)      <- no cgroup ceiling

The container caps the worker at 4 CPUs and 12 GiB, so a host run measures a
machine nobody deploys on.

Usage:
    uv run --extra extraction python scripts/benchmark_extraction.py \
        --parallel 1 data/corp-10k-2025.pdf
    uv run --extra extraction python scripts/benchmark_extraction.py \
        --parallel 3 data/corp-10k-2025.pdf data/annualreport-2025.pdf \
        data/ACTIVISIONBLIZZARD_2015_10K.pdf --json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import Settings, get_settings  # noqa: E402
from app.documents.validation import PDF_MEDIA_TYPE  # noqa: E402
from app.extraction.sandbox import SAFE_ENVIRONMENT_KEYS  # noqa: E402

SAMPLE_INTERVAL_SECONDS = 0.1
"""How often each child's /proc entries are read. Small enough to catch a
peak between page bursts, large enough not to perturb what it measures."""


@dataclass
class Sample:
    """One reading of one child process."""

    rss_bytes: int
    cpu_seconds: float


@dataclass
class ProcessRun:
    """What one sandboxed extraction did, as measured from outside."""

    document: str
    pages: int
    exit_code: int
    wall_seconds: float
    peak_rss_bytes: int = 0
    cpu_seconds: float = 0.0
    error: str = ""

    @property
    def cpu_ratio(self) -> float:
        """CPU-seconds per wall-second. Above 1 means the work was threaded."""
        return self.cpu_seconds / self.wall_seconds if self.wall_seconds else 0.0

    @property
    def pages_per_second(self) -> float:
        return self.pages / self.wall_seconds if self.wall_seconds else 0.0


@dataclass
class BatchReport:
    """One scenario: N documents extracted at the same time."""

    parallel: int
    wall_seconds: float
    runs: list[ProcessRun] = field(default_factory=list)
    peak_total_rss_bytes: int = 0
    host_cpu_count: int = 0
    cgroup_cpu_limit: float | None = None
    cgroup_memory_limit_bytes: int | None = None

    @property
    def total_cpu_seconds(self) -> float:
        return sum(run.cpu_seconds for run in self.runs)

    @property
    def total_pages(self) -> int:
        return sum(run.pages for run in self.runs)

    @property
    def pages_per_second(self) -> float:
        return self.total_pages / self.wall_seconds if self.wall_seconds else 0.0

    @property
    def all_succeeded(self) -> bool:
        return all(run.exit_code == 0 for run in self.runs)


def read_status(pid: int) -> Sample | None:
    """One child's resident memory and consumed CPU, straight from /proc."""
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (OSError, FileNotFoundError):
        return None

    rss_bytes = 0
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            rss_bytes = int(line.split()[1]) * 1024
            break
    else:
        return None

    ticks_per_second = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    fields = stat.rsplit(")", 1)[1].split()
    utime, stime = int(fields[11]), int(fields[12])
    return Sample(rss_bytes=rss_bytes, cpu_seconds=(utime + stime) / ticks_per_second)


def cgroup_limits() -> tuple[float | None, int | None]:
    """The CPU and memory ceilings of the cgroup this process runs in.

    ``None`` means the host, which has no ceiling worth reporting. Both the
    v2 and v1 layouts are read, because the worker image and a developer
    machine disagree about which one they use.
    """
    cpu_limit: float | None = None
    memory_limit: int | None = None

    v2_cpu = Path("/sys/fs/cgroup/cpu.max")
    if v2_cpu.exists():
        quota, period = v2_cpu.read_text().split()
        if quota != "max":
            cpu_limit = int(quota) / int(period)
    else:
        quota_path = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
        period_path = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
        if quota_path.exists() and period_path.exists():
            v1_quota = int(quota_path.read_text())
            if v1_quota > 0:
                cpu_limit = v1_quota / int(period_path.read_text())

    v2_memory = Path("/sys/fs/cgroup/memory.max")
    if v2_memory.exists():
        raw_limit = v2_memory.read_text().strip()
        if raw_limit != "max":
            memory_limit = int(raw_limit)
    else:
        v1_memory = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
        if v1_memory.exists():
            v1_limit = int(v1_memory.read_text())
            if v1_limit < 1 << 60:
                memory_limit = v1_limit

    return cpu_limit, memory_limit


def page_count(path: Path) -> int:
    """Pages the document has, read once up front for the throughput figure."""
    import pymupdf

    with pymupdf.open(path) as pdf:
        return pdf.page_count


def sandbox_environment() -> dict[str, str]:
    """The environment the worker hands its sandbox.

    Copied from ``SandboxedExtractor``: an allowlist of host variables plus
    fixed thread caps. Without the caps OpenBLAS sizes its pool to every host
    core, which both distorts the CPU figure and blows past RLIMIT_NPROC --
    a measurement that does not reproduce this is measuring a different
    program.
    """
    environment = {key: os.environ[key] for key in SAFE_ENVIRONMENT_KEYS if key in os.environ}
    environment.update(
        {
            "MKL_NUM_THREADS": "4",
            "NUMEXPR_NUM_THREADS": "4",
            "OMP_NUM_THREADS": "4",
            "OPENBLAS_NUM_THREADS": "4",
            "MALLOC_ARENA_MAX": "2",
        }
    )
    return environment


def sandbox_command(
    *,
    source: Path,
    output_dir: Path,
    settings: Settings,
    timeout_seconds: int,
) -> list[str]:
    """The command the worker runs, with the worker's own limits."""
    output_dir.mkdir(parents=True, exist_ok=True)
    return [
        sys.executable,
        "-m",
        "app.extraction.sandbox_runner",
        "--input",
        str(source),
        "--media-type",
        PDF_MEDIA_TYPE,
        "--output-dir",
        str(output_dir),
        "--chunk-max-tokens",
        str(settings.chunk_max_tokens),
        "--tokenizer-name",
        settings.chunk_tokenizer,
        "--scan-text-threshold",
        str(settings.native_scan_text_threshold),
        "--no-scan-page-describe",
        "--timeout-seconds",
        str(timeout_seconds),
        "--memory-limit-bytes",
        str(settings.parser_memory_limit_bytes),
        "--max-processes",
        str(settings.parser_max_processes),
        "--max-artifact-bytes",
        str(settings.max_artifact_bytes),
        "--max-chunk-payload-bytes",
        str(settings.max_chunk_payload_bytes),
        "--max-extracted-text-bytes",
        str(settings.max_extracted_text_bytes),
        "--max-chunks",
        str(settings.max_chunks_per_document),
        "--image-render-scale",
        str(settings.image_render_scale),
        "--image-min-pixels",
        str(settings.image_min_pixels),
        "--max-image-bytes",
        str(settings.max_image_bytes),
        "--max-total-image-bytes",
        str(settings.max_total_image_bytes),
    ]


def run_batch(
    documents: list[Path], *, timeout_seconds: int, label: str
) -> BatchReport:
    """Extract every document at once and sample the processes as they work."""
    settings = get_settings()
    cpu_limit, memory_limit = cgroup_limits()
    report = BatchReport(
        parallel=len(documents),
        wall_seconds=0.0,
        host_cpu_count=os.cpu_count() or 0,
        cgroup_cpu_limit=cpu_limit,
        cgroup_memory_limit_bytes=memory_limit,
    )

    with tempfile.TemporaryDirectory(prefix="benchmark-extraction-") as scratch:
        scratch_path = Path(scratch)
        processes: list[tuple[Path, subprocess.Popen[bytes], Path, float]] = []
        print(f"[{label}] starting {len(documents)} sandbox(es)", file=sys.stderr, flush=True)
        started = time.perf_counter()
        for index, document in enumerate(documents):
            working = scratch_path / f"{index}"
            working.mkdir(parents=True, exist_ok=True)
            copy = working / document.name
            shutil.copy2(document, copy)
            output_dir = working / "out"
            log_path = working / "sandbox.log"
            command = sandbox_command(
                source=copy, output_dir=output_dir, settings=settings,
                timeout_seconds=timeout_seconds,
            )
            with log_path.open("wb") as log:
                process = subprocess.Popen(  # noqa: S603 - fixed module, project interpreter
                    command,
                    env=sandbox_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=log,
                    start_new_session=True,
                )
            processes.append((document, process, log_path, time.perf_counter()))

        peaks: dict[int, int] = {process.pid: 0 for _, process, _, _ in processes}
        cpu_seconds: dict[int, float] = {process.pid: 0.0 for _, process, _, _ in processes}
        total_peak = 0
        while any(process.poll() is None for _, process, _, _ in processes):
            live = 0
            for _, process, _, _ in processes:
                if process.poll() is not None:
                    continue
                sample = read_status(process.pid)
                if sample is None:
                    continue
                live += 1
                peaks[process.pid] = max(peaks[process.pid], sample.rss_bytes)
                cpu_seconds[process.pid] = max(cpu_seconds[process.pid], sample.cpu_seconds)
            total_peak = max(total_peak, sum(peaks.values()))
            time.sleep(SAMPLE_INTERVAL_SECONDS if live else 0.02)

        report.wall_seconds = time.perf_counter() - started
        report.peak_total_rss_bytes = total_peak

        for document, process, log_path, launch in processes:
            finished = time.perf_counter()
            code = process.wait()
            if code != 0 and code < 0:
                with suppress_lookup():
                    os.killpg(process.pid, signal.SIGKILL)
            detail = ""
            if code != 0:
                detail = log_path.read_text(errors="replace")[-400:] if log_path.exists() else ""
            report.runs.append(
                ProcessRun(
                    document=document.name,
                    pages=page_count(document),
                    exit_code=code,
                    wall_seconds=finished - launch,
                    peak_rss_bytes=peaks.get(process.pid, 0),
                    cpu_seconds=cpu_seconds.get(process.pid, 0.0),
                    error=detail.strip(),
                )
            )

    return report


class suppress_lookup:
    """Swallow a process that already exited before the group kill."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, kind: object, value: object, traceback: object) -> bool:
        return isinstance(value, ProcessLookupError)


def human_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    scaled = float(value)
    for unit in ("KiB", "MiB", "GiB"):
        scaled /= 1024
        if scaled < 1024 or unit == "GiB":
            return f"{scaled:.1f} {unit}"
    return f"{scaled:.1f} GiB"


def render(report: BatchReport) -> str:
    environment = "host"
    if report.cgroup_cpu_limit or report.cgroup_memory_limit_bytes:
        parts = []
        if report.cgroup_cpu_limit:
            parts.append(f"{report.cgroup_cpu_limit:.1f} cpu")
        if report.cgroup_memory_limit_bytes:
            parts.append(human_bytes(report.cgroup_memory_limit_bytes))
        environment = "cgroup (" + ", ".join(parts) + ")"

    lines = [
        "=" * 78,
        f"PARALLEL EXTRACTION  parallel={report.parallel}  env={environment}",
        "=" * 78,
        f"  host cpu count      {report.host_cpu_count}",
        f"  wall clock          {report.wall_seconds:.1f}s",
        f"  pages total         {report.total_pages}",
        f"  throughput          {report.pages_per_second:.1f} pages/s",
        f"  peak RSS (batch)    {human_bytes(report.peak_total_rss_bytes)}",
        f"  CPU-seconds total   {report.total_cpu_seconds:.0f}s",
        (
            f"  CPU / wall          {report.total_cpu_seconds / report.wall_seconds:.2f}x"
            if report.wall_seconds
            else "  CPU / wall          n/a"
        ),
        "",
        f"  {'document':<32} {'pages':>5} {'wall':>8} {'peak RSS':>11} "
        f"{'cpu-s':>7} {'cpu/wall':>9}",
    ]
    for run in sorted(report.runs, key=lambda item: item.document):
        lines.append(
            f"  {run.document[:32]:<32} {run.pages:>5} {run.wall_seconds:>7.1f}s "
            f"{human_bytes(run.peak_rss_bytes):>11} {run.cpu_seconds:>7.0f} "
            f"{run.cpu_ratio:>8.2f}x"
        )
    for run in report.runs:
        if run.exit_code != 0:
            lines.append("")
            lines.append(f"  FAILED {run.document} (exit {run.exit_code})")
            if run.error:
                lines.append("  " + run.error.replace("\n", "\n  ")[:600])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("documents", type=Path, nargs="+", help="PDF files to extract")
    parser.add_argument(
        "--parallel",
        type=int,
        default=0,
        help="how many to run at once (default: one batch per document)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=3600,
        help=(
            "passed through as the sandbox's RLIMIT_CPU, so it is CPU-SECONDS "
            "summed over every thread, not wall-clock. The worker default is "
            "3600 for this reason: a 410-page document spends ~890 CPU-seconds "
            "on 4 cores, so a smaller value kills it with SIGXCPU (exit -24)"
        ),
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    for document in args.documents:
        if not document.is_file():
            raise SystemExit(f"no such file: {document}")

    batches: list[BatchReport] = []
    if args.parallel and args.parallel > 0:
        size = args.parallel
        groups = [
            args.documents[index : index + size]
            for index in range(0, len(args.documents), size)
        ]
        for group in groups:
            batches.append(
                run_batch(
                    group,
                    timeout_seconds=args.timeout_seconds,
                    label=f"parallel={len(group)}",
                )
            )
    else:
        for document in args.documents:
            batches.append(
                run_batch([document], timeout_seconds=args.timeout_seconds, label="serial")
            )

    if args.json:
        print(
            json.dumps(
                [
                    {
                        **asdict(batch),
                        "total_cpu_seconds": batch.total_cpu_seconds,
                        "total_pages": batch.total_pages,
                        "pages_per_second": batch.pages_per_second,
                        "all_succeeded": batch.all_succeeded,
                    }
                    for batch in batches
                ],
                indent=2,
            )
        )
    else:
        for batch in batches:
            print(render(batch))
            print()

    if not all(batch.all_succeeded for batch in batches):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
