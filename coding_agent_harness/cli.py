from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from pydantic import Field, ValidationError

from .models import (
    StrictModel,
    TaskPacket,
    VerificationCommand,
)
from .orchestrator import TaskRunResult, run_task
from .worker import QwenWorker


class TaskFile(StrictModel):
    task: TaskPacket
    verification_commands: list[VerificationCommand] = Field(
        default_factory=list,
    )


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "must be a number"
        ) from exc

    if number <= 0:
        raise argparse.ArgumentTypeError(
            "must be greater than zero"
        )

    return number


def _load_task_file(
    path: Path,
) -> TaskFile:
    payload = json.loads(
        path.read_text(
            encoding="utf-8",
        )
    )

    return TaskFile.model_validate(
        payload
    )


def _print_result(
    result: TaskRunResult,
) -> None:
    print(
        f"Task: {result.task_id}"
    )
    print(
        f"Status: {result.status}"
    )
    print(
        f"Baseline: {result.baseline_sha}"
    )
    print(
        f"Final HEAD: {result.final_head_sha}"
    )

    process = result.worker.process

    print(
        "Worker process: "
        f"exit={process.exit_code} "
        f"timed_out={process.timed_out}"
    )

    completion = result.worker.completion

    if completion is None:
        print(
            "Worker completion: unavailable"
        )

        if result.worker.completion_error:
            print(
                "Completion error: "
                f"{result.worker.completion_error}"
            )
    else:
        print(
            "Worker completion: "
            f"{completion.status}"
        )
        print(
            f"Worker summary: {completion.summary}"
        )

    print("Changed files:")

    if result.changed_files:
        for path in result.changed_files:
            print(
                f"  - {path}"
            )
    else:
        print(
            "  (none)"
        )

    print("Findings:")

    if result.findings:
        for finding in result.findings:
            print(
                f"  - [{finding.code}] "
                f"{finding.detail}"
            )
    else:
        print(
            "  (none)"
        )

    print(
        "Verification: "
        + (
            "PASS"
            if result.verification.passed
            else "FAIL"
        )
    )

    print("Patch:")

    if result.patch:
        print(
            result.patch,
            end=(
                ""
                if result.patch.endswith("\n")
                else "\n"
            ),
        )
    else:
        print(
            "  (empty)"
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coding-harness",
        description=(
            "Run a bounded coding-agent task "
            "and collect independent evidence."
        ),
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    run_parser = subparsers.add_parser(
        "run",
        help="Run one bounded Qwen task.",
    )

    run_parser.add_argument(
        "task_file",
        type=Path,
        help=(
            "JSON task specification."
        ),
    )

    run_parser.add_argument(
        "--repo",
        required=True,
        type=Path,
        help=(
            "Repository or worktree in which "
            "the worker should operate."
        ),
    )

    run_parser.add_argument(
        "--model",
        required=True,
        help=(
            "Qwen model identifier passed to "
            "the worker."
        ),
    )

    run_parser.add_argument(
        "--qwen-command",
        default="qwen",
        help=(
            "Qwen executable. "
            "Defaults to 'qwen'."
        ),
    )

    run_parser.add_argument(
        "--worker-timeout",
        type=_positive_float,
        default=600.0,
        help=(
            "Worker timeout in seconds. "
            "Defaults to 600."
        ),
    )

    return parser


def app(
    argv: Sequence[str] | None = None,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command != "run":
        parser.error(
            "unsupported command"
        )

    task_file = args.task_file.resolve()
    repo_dir = args.repo.resolve()

    if not repo_dir.is_dir():
        print(
            f"Repository does not exist: {repo_dir}",
            file=sys.stderr,
        )
        return 1

    try:
        spec = _load_task_file(
            task_file
        )
    except (
        OSError,
        json.JSONDecodeError,
        ValidationError,
    ) as exc:
        print(
            f"Task file error: {exc}",
            file=sys.stderr,
        )
        return 1

    try:
        worker = QwenWorker(
            command=[
                args.qwen_command,
            ],
            model=args.model,
            timeout_seconds=(
                args.worker_timeout
            ),
        )

        result = run_task(
            task=spec.task,
            repo_dir=repo_dir,
            worker=worker,
            verification_commands=(
                spec.verification_commands
            ),
        )
    except Exception as exc:
        print(
            f"Harness run failed: {exc}",
            file=sys.stderr,
        )
        return 1

    _print_result(
        result
    )

    if result.status == "ready-for-review":
        return 0

    return 2