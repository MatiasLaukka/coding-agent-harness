from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Sequence

from pydantic import BaseModel, Field, ValidationError

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


def _print_failure_evidence(exc: BaseException) -> None:
    """Expose completed observations without echoing command/prompt arguments."""
    def serializable(value):
        if isinstance(value, TaskPacket):
            return {"task_id": value.task_id}
        if isinstance(value, BaseModel):
            return serializable(value.model_dump(mode="json"))
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key == "argv":
                    raw = json.dumps(item, ensure_ascii=False).encode("utf-8")
                    result["argv_sha256"] = hashlib.sha256(raw).hexdigest()
                else:
                    result[key] = serializable(item)
            return result
        if isinstance(value, list):
            return [serializable(item) for item in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return {"unavailable_type": type(value).__name__}

    try:
        evidence = getattr(exc, "harness_evidence", {"failure_type": type(exc).__name__})
        print("Failure evidence: " + json.dumps(serializable(evidence), ensure_ascii=False), file=sys.stderr)
    except Exception as capture_error:
        print("Failure evidence unavailable: " + type(capture_error).__name__, file=sys.stderr)


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

    handoff_parser = subparsers.add_parser(
        "handoff", help="Validate supervisor documents; never execute proposals.")
    handoff_commands = handoff_parser.add_subparsers(dest="handoff_command", required=True)
    for name in ("validate", "preflight"):
        command = handoff_commands.add_parser(name)
        command.add_argument("proposal", type=Path)
        command.add_argument("--repo", required=True, type=Path)
        command.add_argument("--handoff", type=Path)
        command.add_argument("--approval", type=Path, required=name == "preflight")
        command.add_argument("--approval-dir", type=Path, required=name == "preflight")
        command.add_argument("--untrusted-root", type=Path, action="append", default=[])
        if name == "validate":
            command.add_argument("--output", type=Path)
        else:
            command.add_argument("--task-file", type=Path, required=True)
            command.add_argument("--execution-config", type=Path, required=True)

    return parser


def app(
    argv: Sequence[str] | None = None,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "handoff":
        return _handoff_app(args)

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
    except KeyboardInterrupt as exc:
        print("Harness run interrupted.", file=sys.stderr)
        _print_failure_evidence(exc)
        return 130
    except Exception as exc:
        print(
            f"Harness run failed: {exc}",
            file=sys.stderr,
        )
        _print_failure_evidence(exc)
        return 1

    _print_result(
        result
    )

    if result.status == "ready-for-review":
        return 0

    return 2


def _handoff_app(args) -> int:
    from .supervisor import (
        ExecutionConfig, HandoffError, ValidationFeedback, execution_preflight,
        load_document, load_handoff, load_operator_approval, load_proposal,
        materialize_task_file, validate_proposal, _write_output,
    )

    try:
        if bool(args.approval) != bool(args.approval_dir):
            raise HandoffError("approval_directory_required")
        proposal = load_proposal(args.proposal)
        handoff = load_handoff(args.handoff) if args.handoff else None
        if args.handoff_command == "preflight":
            spec = load_document(args.task_file, TaskFile)
            config = load_document(args.execution_config, ExecutionConfig)
            feedback = execution_preflight(
                proposal, handoff=handoff, approval_path=args.approval,
                trusted_approval_dir=args.approval_dir,
                untrusted_workspace_roots=args.untrusted_root, repo_dir=args.repo,
                task=spec.task, verification_commands=spec.verification_commands,
                execution_config=config)
            input_errors = {
                "invalid_document", "document_too_large", "parent_binding_mismatch",
                "parent_handoff_required", "unsupported_acceptance",
            }
            code = (0 if feedback.passed else
                    1 if input_errors.intersection(feedback.errors) else 2)
        else:
            approval = load_operator_approval(
                args.approval, trusted_dir=args.approval_dir, target_repo=args.repo,
                untrusted_workspace_roots=args.untrusted_root) if args.approval else None
            feedback = validate_proposal(proposal, handoff=handoff, approval=approval)
            code = (2 if feedback.status == "needs_operator_approval" else
                    1 if feedback.status == "invalid" else 0)
            if args.output:
                if feedback.status == "approval_validated":
                    _write_output(args.output, materialize_task_file(
                        proposal, handoff=handoff, approval=approval))
                elif feedback.status == "nonexecuting_recommendation":
                    raise HandoffError("task_not_approved")
        print(json.dumps(feedback.model_dump(mode="json"), sort_keys=True))
        return code
    except HandoffError as error:
        feedback = ValidationFeedback(status="invalid", errors=[str(error)])
    except (OSError, ValueError, TypeError):
        feedback = ValidationFeedback(status="invalid", errors=["invalid_document"])
    print(json.dumps(feedback.model_dump(mode="json"), sort_keys=True))
    return 1
