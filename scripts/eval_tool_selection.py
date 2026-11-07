#!/usr/bin/env python
"""Measure whether the agent picks the right tool, before it picks a bad one.

Adding a tool is cheap; making a model choose correctly among similar ones is
not. Studies of tool selection agree on the shape of the failure: accuracy
falls as the catalogue grows, and falls faster when the added tools resemble
the ones already there. This harness measures that for *our* catalogue and
*our* configured model, because tolerance to tool count is model-specific --
a number from a benchmark does not transfer to the model an admin picked.

The unit of measurement is one model turn: a question, the real system
prompt, the real tool specs, and one completion. Nothing is executed, so
retrieval quality and database state cannot flatter or spoil the result. Two
numbers come out of a turn, because they answer different questions:

* **first call** -- did the model reach for the right tool immediately? This
  is the strict selection metric the studies measure.
* **coverage** -- did the expected tool appear anywhere in the turn? An agent
  may legitimately orient with ``list_workspaces`` or resolve an id with
  ``find_documents`` before acting, and a first-call-only metric would score
  that plan as a miss.

The gap between the two is the cost of the model's own workflow. A large gap
means the catalogue is not confusing the model, the *prompt* is encouraging
extra steps; a low coverage means a genuine selection problem.

Prompts marked ``two_turn`` get one follow-up turn. A document tool cannot be
chosen before a document id exists, so when the first turn only resolves one,
the harness feeds a canned ``find_documents``/``list_queryable_files`` result
and records the next call. Coverage counts both turns; first-call accuracy
still counts only the first.

Run it:

    uv run python scripts/eval_tool_selection.py --dry-run
    uv run python scripts/eval_tool_selection.py --limit 5 --repeats 1
    uv run python scripts/eval_tool_selection.py --repeats 3
    uv run python scripts/eval_tool_selection.py --from-json evals/<run>.json

The model, base URL and key come from the ``agent_model_settings`` row the
Chat Agent uses -- the same configuration a browser user gets. A prompt file
is JSONL; each line is one question with its expected tool, and optionally a
set of acceptable tools for questions where more than one answer is honest.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.agents.chat import (  # noqa: E402
    ChatProviderError,
    ChatTurn,
    OpenAICompatibleChatProvider,
)
from app.agents.harness import _system_prompt, _tool_specs  # noqa: E402
from app.agents.model_settings import (  # noqa: E402
    load_agent_model_config,
    provider_for,
)
from app.core.config import Settings  # noqa: E402

DEFAULT_PROMPTS = Path("evals/tool_selection.jsonl")
DEFAULT_REPEATS = 3
NO_TOOL = "none"


@dataclass(frozen=True)
class Prompt:
    """One question and the call a careful reader of the catalogue would make."""

    id: str
    question: str
    expected: str
    category: str
    difficulty: str = "clear"
    acceptable: tuple[str, ...] = ()
    args: dict[str, str] = field(default_factory=dict)
    notes: str = ""
    two_turn: bool = False


@dataclass
class Sample:
    """One model turn: what it chose, and whether that was the right choice."""

    prompt: Prompt
    repeat: int
    predicted: str
    calls: list[str]
    arguments: dict[str, Any]
    correct: bool
    covered: bool
    required_ok: bool
    latency_ms: float
    followup_calls: list[str] = field(default_factory=list)
    error: str | None = None


def load_prompts(path: Path) -> list[Prompt]:
    prompts: list[Prompt] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except ValueError as exc:
            raise SystemExit(f"{path}:{line_number}: not valid JSON: {exc}") from exc
        try:
            prompts.append(
                Prompt(
                    id=str(raw["id"]),
                    question=str(raw["question"]),
                    expected=str(raw["expected"]),
                    category=str(raw["category"]),
                    difficulty=str(raw.get("difficulty", "clear")),
                    acceptable=tuple(str(name) for name in raw.get("acceptable", [])),
                    args={str(key): str(value) for key, value in raw.get("args", {}).items()},
                    notes=str(raw.get("notes", "")),
                    two_turn=bool(raw.get("two_turn", False)),
                )
            )
        except KeyError as exc:
            raise SystemExit(f"{path}:{line_number}: missing field {exc}") from exc
    return prompts


def tool_names(specs: list[dict[str, Any]]) -> list[str]:
    return [spec["function"]["name"] for spec in specs]


def required_arguments(specs: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {
        spec["function"]["name"]: list(
            spec["function"].get("parameters", {}).get("required", [])
        )
        for spec in specs
    }


def validate(prompts: list[Prompt], names: list[str]) -> list[str]:
    """Reject a prompt set that cannot be scored against this catalogue."""
    errors: list[str] = []
    known = set(names)
    seen: set[str] = set()
    for prompt in prompts:
        where = f"prompt {prompt.id!r}"
        if prompt.id in seen:
            errors.append(f"{where}: duplicate id")
        seen.add(prompt.id)
        if prompt.difficulty not in {"clear", "ambiguous"}:
            errors.append(f"{where}: difficulty must be clear or ambiguous")
        if prompt.difficulty == "ambiguous" and not prompt.acceptable:
            errors.append(f"{where}: an ambiguous prompt needs an acceptable set")
        if prompt.expected != NO_TOOL and prompt.expected not in known:
            errors.append(f"{where}: expected tool {prompt.expected!r} is not in the catalogue")
        for name in prompt.acceptable:
            if name not in known:
                errors.append(f"{where}: acceptable tool {name!r} is not in the catalogue")
    return errors


def is_correct(prompt: Prompt, predicted: str) -> bool:
    """Whether the *first* call was the expected one."""
    if prompt.expected == NO_TOOL:
        return predicted == NO_TOOL
    if prompt.difficulty == "ambiguous":
        return predicted in prompt.acceptable
    return predicted == prompt.expected


def is_covered(prompt: Prompt, calls: list[str], followup: list[str] | None = None) -> bool:
    """Whether the expected call appeared in the turn or its follow-up."""
    seen = [*calls, *(followup or [])]
    if prompt.expected == NO_TOOL:
        return not calls
    if prompt.difficulty == "ambiguous":
        return any(name in prompt.acceptable for name in seen)
    return prompt.expected in seen


def arguments_ok(
    prompt: Prompt,
    predicted: str,
    arguments: dict[str, Any],
    required: dict[str, list[str]],
) -> bool:
    """Required names present and non-empty, plus the values the question gave.

    A single cold turn cannot know a real document_id, so this checks presence
    rather than existence; where the question itself carries the value, the
    check is a substring match against the argument the model produced.
    """
    if predicted == NO_TOOL:
        return True
    if predicted not in required:
        return False
    for name in required[predicted]:
        value = arguments.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            return False
    for name, wanted in prompt.args.items():
        value = arguments.get(name)
        if not isinstance(value, str) or wanted.lower() not in value.lower():
            return False
    return True


DOC_ID = "11111111-1111-4111-8111-111111111111"
WORKSPACE_ID = "22222222-2222-4222-8222-222222222222"


async def complete_turn(
    provider: OpenAICompatibleChatProvider,
    messages: list[dict[str, Any]],
    specs: list[dict[str, Any]],
    temperature: float,
) -> tuple[ChatTurn, float]:
    started = time.perf_counter()
    turn = await provider.complete(
        messages=messages,
        tools=specs,
        temperature=temperature,
    )
    return turn, (time.perf_counter() - started) * 1000


def assistant_tool_message(turn: ChatTurn) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": turn.content or "",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments),
                },
            }
            for call in turn.tool_calls
        ],
    }


def canned_result(name: str) -> str:
    """A plausible tool result, so a dependent step can be measured.

    The eval does not touch the database; these results exist only to unblock
    the second call. They carry the same synthetic ids the questions use, so a
    model that reads the result can act on it exactly as it would in a real
    conversation.
    """
    if name == "find_documents":
        return (
            "Documents:\n"
            f"  [{DOC_ID}] manual.pdf  workspace_id={WORKSPACE_ID}  "
            "status=ready  1024 bytes\n"
        )
    if name == "list_workspaces":
        return (
            "Workspaces (1):\n"
            f"  [{WORKSPACE_ID}] legal — Legal (owner)  1 document(s)\n"
        )
    if name == "list_queryable_files":
        return (
            "CSV files:\n"
            f"  invoices.csv  [{DOC_ID}]  10 rows  columns: vendor, amount\n"
        )
    if name == "list_images":
        return (
            "3 picture(s):\n"
            "image-p3-0 page=3 image/png 1024B described caption=Revenue chart\n"
        )
    if name == "search_context":
        return (
            '[1] filename="manual.pdf" ordinal=31 ...\n'
            "<untrusted_document_text>\n...\n</untrusted_document_text>"
        )
    if name == "get_document_outline":
        return (
            'filename="manual.pdf" ... sections=2\n'
            "  1. Introduction  [ordinal 0, page 1, 1 chunk(s)]\n"
        )
    return "ok"


def tool_messages(turn: ChatTurn) -> list[dict[str, Any]]:
    return [
        {
            "role": "tool",
            "tool_call_id": call.id,
            "content": canned_result(call.name),
        }
        for call in turn.tool_calls
    ]


async def run_sample(
    provider: OpenAICompatibleChatProvider,
    prompt: Prompt,
    *,
    repeat: int,
    system_prompt: str,
    specs: list[dict[str, Any]],
    required: dict[str, list[str]],
    temperature: float,
    retries: int,
) -> Sample:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt.question},
    ]
    for attempt in range(retries + 1):
        try:
            turn, latency_ms = await complete_turn(
                provider, messages, specs, temperature
            )
        except ChatProviderError as exc:
            if not exc.retryable or attempt == retries:
                return Sample(
                    prompt=prompt,
                    repeat=repeat,
                    predicted="error",
                    calls=[],
                    arguments={},
                    correct=False,
                    covered=False,
                    required_ok=False,
                    latency_ms=0.0,
                    error=str(exc),
                )
            await asyncio.sleep(1.5 * (attempt + 1))
            continue

        calls = [call.name for call in turn.tool_calls]
        predicted = calls[0] if calls else NO_TOOL
        arguments = turn.tool_calls[0].arguments if turn.tool_calls else {}
        followup_calls: list[str] = []
        error: str | None = None
        if prompt.two_turn and turn.tool_calls and not is_covered(prompt, calls):
            follow_messages = [
                *messages,
                assistant_tool_message(turn),
                *tool_messages(turn),
            ]
            try:
                followup, _ = await complete_turn(
                    provider, follow_messages, specs, temperature
                )
            except ChatProviderError as exc:
                error = f"follow-up: {exc}"
            else:
                followup_calls = [call.name for call in followup.tool_calls]

        return Sample(
            prompt=prompt,
            repeat=repeat,
            predicted=predicted,
            calls=calls,
            arguments=arguments,
            correct=is_correct(prompt, predicted),
            covered=is_covered(prompt, calls, followup_calls),
            required_ok=arguments_ok(prompt, predicted, arguments, required),
            latency_ms=latency_ms,
            followup_calls=followup_calls,
            error=error,
        )
    raise AssertionError("the retry loop must return")


def _rate(part: int, whole: int) -> float:
    return part / whole if whole else 0.0


def summarise(
    samples: list[Sample],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, int]]]:
    clear = [sample for sample in samples if sample.prompt.difficulty == "clear"]
    ambiguous = [sample for sample in samples if sample.prompt.difficulty == "ambiguous"]
    needed = [sample for sample in clear if sample.prompt.expected != NO_TOOL]
    no_tool = [sample for sample in clear if sample.prompt.expected == NO_TOOL]
    right_tool = [sample for sample in clear if sample.predicted == sample.prompt.expected]
    wrong_tool = [
        sample
        for sample in needed
        if sample.predicted not in {sample.prompt.expected, NO_TOOL}
    ]
    no_call = [sample for sample in needed if sample.predicted == NO_TOOL]
    over_call = [sample for sample in no_tool if sample.calls]
    orientation = [
        sample
        for sample in needed
        if sample.calls[:1] == ["list_workspaces"]
        and sample.prompt.expected != "list_workspaces"
    ]
    followup_needed = [
        sample
        for sample in clear
        if sample.prompt.two_turn and not is_covered(sample.prompt, sample.calls)
    ]
    followup_ok = [
        sample
        for sample in followup_needed
        if sample.prompt.expected in sample.followup_calls
    ]

    per_tool: dict[str, Any] = {}
    by_expected: dict[str, list[Sample]] = defaultdict(list)
    for sample in clear:
        by_expected[sample.prompt.expected].append(sample)
    for name, rows in sorted(by_expected.items()):
        correct = [sample for sample in rows if sample.correct]
        covered = [sample for sample in rows if sample.covered]
        per_tool[name] = {
            "n": len(rows),
            "first": len(correct),
            "first_accuracy": _rate(len(correct), len(rows)),
            "covered": len(covered),
            "coverage": _rate(len(covered), len(rows)),
            "avg_calls": sum(len(sample.calls) for sample in rows) / len(rows),
            "confused_with": dict(
                Counter(sample.predicted for sample in rows if not sample.correct)
            ),
        }

    confusion: dict[str, dict[str, int]] = {}
    for sample in clear:
        bucket = confusion.setdefault(sample.prompt.expected, {})
        bucket[sample.predicted] = bucket.get(sample.predicted, 0) + 1

    summary = {
        "samples": len(samples),
        "errors": sum(1 for sample in samples if sample.error),
        "first_call_accuracy": _rate(sum(sample.correct for sample in clear), len(clear)),
        "coverage": _rate(sum(sample.covered for sample in clear), len(clear)),
        "clear_n": len(clear),
        "ambiguous_accuracy": _rate(
            sum(sample.covered for sample in ambiguous), len(ambiguous)
        ),
        "ambiguous_n": len(ambiguous),
        "no_call_rate": _rate(len(no_call), len(needed)),
        "wrong_tool_rate": _rate(len(wrong_tool), len(needed)),
        "over_call_rate": _rate(len(over_call), len(no_tool)),
        "orientation_rate": _rate(len(orientation), len(needed)),
        "followup_n": len(followup_needed),
        "followup_ok": len(followup_ok),
        "followup_accuracy": _rate(len(followup_ok), len(followup_needed)),
        "avg_calls": (
            sum(len(sample.calls) for sample in samples) / len(samples) if samples else 0.0
        ),
        "required_args_rate": _rate(
            sum(sample.required_ok for sample in right_tool), len(right_tool)
        ),
        "avg_latency_ms": (
            sum(sample.latency_ms for sample in samples) / len(samples) if samples else 0.0
        ),
    }
    return summary, per_tool, confusion


def print_report(
    *,
    config_model: str,
    config_provider: str,
    prompts: list[Prompt],
    samples: list[Sample],
    summary: dict[str, Any],
    per_tool: dict[str, Any],
    confusion: dict[str, dict[str, int]],
    repeats: int,
    temperature: float,
) -> None:
    clear = [sample for sample in samples if sample.prompt.difficulty == "clear"]
    print(f"\nTool selection — {config_model} ({config_provider})")
    print(
        f"prompts {len(prompts)} · repeats {repeats} · samples {summary['samples']} "
        f"· errors {summary['errors']} · temperature {temperature}"
    )
    print()
    print("| metric | value |")
    print("| --- | --- |")
    print(
        f"| first-call accuracy | {summary['first_call_accuracy']:.1%} "
        f"({sum(sample.correct for sample in clear)}/{summary['clear_n']}) |"
    )
    print(
        f"| coverage (any call) | {summary['coverage']:.1%} "
        f"({sum(sample.covered for sample in clear)}/{summary['clear_n']}) |"
    )
    if summary["followup_n"]:
        print(
            f"| follow-up accuracy | {summary['followup_accuracy']:.1%} "
            f"({summary['followup_ok']}/{summary['followup_n']}) |"
        )
    print(
        f"| ambiguous accuracy | {summary['ambiguous_accuracy']:.1%} "
        f"({sum(s.covered for s in samples if s.prompt.difficulty == 'ambiguous')}"
        f"/{summary['ambiguous_n']}) |"
    )
    print(f"| no-call rate | {summary['no_call_rate']:.1%} |")
    print(f"| wrong-tool rate | {summary['wrong_tool_rate']:.1%} |")
    print(f"| over-call rate | {summary['over_call_rate']:.1%} |")
    print(f"| leading orientation | {summary['orientation_rate']:.1%} |")
    print(f"| calls per turn | {summary['avg_calls']:.2f} |")
    print(f"| required args | {summary['required_args_rate']:.1%} |")
    print(f"| avg latency | {summary['avg_latency_ms']:.0f} ms |")
    print()
    print("| tool | n | first | coverage | calls | confused with |")
    print("| --- | --- | --- | --- | --- | --- |")
    for name, entry in per_tool.items():
        confused = ", ".join(
            f"{other}({count})" for other, count in entry["confused_with"].items()
        )
        print(
            f"| `{name}` | {entry['n']} | {entry['first_accuracy']:.1%} | "
            f"{entry['coverage']:.1%} | {entry['avg_calls']:.2f} | {confused or '-'} |"
        )
    pairs = [
        (count, expected, predicted)
        for expected, counts in confusion.items()
        for predicted, count in counts.items()
        if predicted != expected
    ]
    pairs.sort(reverse=True)
    if pairs:
        print()
        print("Top first-call confusions:")
        for count, expected, predicted in pairs[:10]:
            print(f"  {expected} -> {predicted}  ({count})")


def build_toolset(name: str, settings: Settings) -> tuple[list[dict[str, Any]], str]:
    """The catalogue and prompt a variant is evaluated with.

    A variant is a package: dropping a tool from the specs while the system
    prompt still names it would measure the prompt, not the catalogue. Add a
    merged variant here together with the prompt that matches it, so an A/B
    run differs in one dimension only.
    """
    if name != "current":
        raise SystemExit(
            f"unknown toolset {name!r}; only 'current' exists so far. Add a "
            "builder here when a merged variant is ready."
        )
    return _tool_specs(row_queries=bool(settings.sql_database_url)), _system_prompt()


def default_output(model: str) -> Path:
    slug = "".join(character if character.isalnum() else "-" for character in model)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return Path("evals") / f"tool-selection-{slug.strip('-')}-{stamp}.json"


def payload_of(
    *,
    config_model: str,
    config_provider: str,
    config_base_url: str,
    args: argparse.Namespace,
    names: list[str],
    summary: dict[str, Any],
    per_tool: dict[str, Any],
    confusion: dict[str, dict[str, int]],
    samples: list[Sample],
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "model": {
            "provider": config_provider,
            "model": config_model,
            "base_url": config_base_url,
        },
        "toolset": args.toolset,
        "tools": names,
        "repeats": args.repeats,
        "temperature": args.temperature,
        "summary": summary,
        "per_tool": per_tool,
        "confusion": confusion,
        "records": [
            {
                "id": sample.prompt.id,
                "question": sample.prompt.question,
                "expected": sample.prompt.expected,
                "difficulty": sample.prompt.difficulty,
                "two_turn": sample.prompt.two_turn,
                "predicted": sample.predicted,
                "calls": sample.calls,
                "arguments": sample.arguments,
                "correct": sample.correct,
                "covered": sample.covered,
                "followup_calls": sample.followup_calls,
                "required_args_ok": sample.required_ok,
                "latency_ms": round(sample.latency_ms, 1),
                "repeat": sample.repeat,
                "error": sample.error,
            }
            for sample in samples
        ],
    }


def samples_from_payload(payload: dict[str, Any]) -> list[Sample]:
    """Re-score a stored run without calling the model again."""
    samples: list[Sample] = []
    for record in payload["records"]:
        prompt = Prompt(
            id=str(record["id"]),
            question=str(record.get("question", "")),
            expected=str(record["expected"]),
            category="",
            difficulty=str(record.get("difficulty", "clear")),
            two_turn=bool(record.get("two_turn", False)),
        )
        calls = [str(name) for name in record.get("calls", [])]
        followup_calls = [str(name) for name in record.get("followup_calls", [])]
        ambiguous = prompt.difficulty == "ambiguous"
        samples.append(
            Sample(
                prompt=prompt,
                repeat=int(record.get("repeat", 0)),
                predicted=str(record["predicted"]),
                calls=calls,
                arguments=dict(record.get("arguments") or {}),
                # A stored record keeps the verdicts the live run computed;
                # the acceptable set is not in the file, so re-deriving them
                # for an ambiguous prompt would silently score it wrong.
                correct=(
                    bool(record.get("correct"))
                    if ambiguous
                    else is_correct(prompt, str(record["predicted"]))
                ),
                covered=(
                    bool(record.get("covered", record.get("correct")))
                    if ambiguous
                    else is_covered(prompt, calls, followup_calls)
                ),
                required_ok=bool(record.get("required_args_ok", False)),
                latency_ms=float(record.get("latency_ms", 0.0)),
                followup_calls=followup_calls,
                error=record.get("error"),
            )
        )
    return samples


def analyse(path: Path) -> int:
    payload = json.loads(path.read_text(encoding="utf-8"))
    samples = samples_from_payload(payload)
    summary, per_tool, confusion = summarise(samples)
    unique: dict[str, Prompt] = {}
    for sample in samples:
        unique.setdefault(sample.prompt.id, sample.prompt)
    prompts = list(unique.values())
    model = payload.get("model", {})
    print_report(
        config_model=str(model.get("model", "unknown")),
        config_provider=str(model.get("provider", "unknown")),
        prompts=prompts,
        samples=samples,
        summary=summary,
        per_tool=per_tool,
        confusion=confusion,
        repeats=int(payload.get("repeats", 1)),
        temperature=float(payload.get("temperature", 0.0)),
    )
    return 0


async def run(args: argparse.Namespace) -> int:
    if args.from_json:
        return analyse(Path(args.from_json))

    settings = Settings()
    specs, system_prompt = build_toolset(args.toolset, settings)
    names = tool_names(specs)
    required = required_arguments(specs)

    prompts = load_prompts(Path(args.prompts))
    if args.limit:
        prompts = prompts[: args.limit]
    errors = validate(prompts, names)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 2

    if args.dry_run:
        counts = Counter(prompt.expected for prompt in prompts)
        print(f"toolset {args.toolset}: {len(names)} tools")
        print(f"prompts: {len(prompts)}")
        for name, count in sorted(counts.items()):
            print(f"  {name:24} {count}")
        print("dry run: no model calls made")
        return 0

    engine = create_async_engine(settings.database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            config = await load_agent_model_config(session)
    finally:
        await engine.dispose()
    if config is None:
        print(
            "error: no chat model is configured; set one in the dashboard first",
            file=sys.stderr,
        )
        return 2

    provider = provider_for(config, f"eval-tool-selection-{uuid4()}")
    print(
        f"model {config.model} ({config.provider}) · {len(prompts)} prompts "
        f"· {args.repeats} repeat(s)"
    )
    samples: list[Sample] = []
    try:
        for index, prompt in enumerate(prompts, 1):
            for repeat in range(args.repeats):
                sample = await run_sample(
                    provider,
                    prompt,
                    repeat=repeat,
                    system_prompt=system_prompt,
                    specs=specs,
                    required=required,
                    temperature=args.temperature,
                    retries=args.retries,
                )
                samples.append(sample)
                mark = "ok" if sample.correct else "MISS"
                detail = sample.predicted
                if not sample.correct:
                    detail = f"{sample.predicted} (want {prompt.expected})"
                print(f"  [{index:>3}/{len(prompts)}] {mark:4} {prompt.id:10} {detail}")
    finally:
        await provider.aclose()

    summary, per_tool, confusion = summarise(samples)

    print_report(
        config_model=config.model,
        config_provider=config.provider,
        prompts=prompts,
        samples=samples,
        summary=summary,
        per_tool=per_tool,
        confusion=confusion,
        repeats=args.repeats,
        temperature=args.temperature,
    )

    output = Path(args.out) if args.out else default_output(config.model)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = payload_of(
        config_model=config.model,
        config_provider=config.provider,
        config_base_url=config.base_url,
        args=args,
        names=names,
        summary=summary,
        per_tool=per_tool,
        confusion=confusion,
        samples=samples,
    )
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nwrote {output}")

    if args.min_accuracy is not None and summary["first_call_accuracy"] < args.min_accuracy:
        print(
            f"error: first-call accuracy {summary['first_call_accuracy']:.1%} is "
            f"below the required {args.min_accuracy:.1%}",
            file=sys.stderr,
        )
        return 1
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompts", default=str(DEFAULT_PROMPTS))
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--toolset", default="current")
    parser.add_argument("--out", default="")
    parser.add_argument("--min-accuracy", type=float, default=None)
    parser.add_argument(
        "--from-json",
        default="",
        help="re-score an existing run instead of calling the model",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate the prompt set against the catalogue without calling the model",
    )
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    return args


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(sys.argv[1:] if argv is None else argv)))


if __name__ == "__main__":
    raise SystemExit(main())
