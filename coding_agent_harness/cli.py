from __future__ import annotations

import argparse
import builtins
import hashlib
import json
import re
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
from .worker import QwenWorker, WorkerRunResult
from .git import GitBaseline, GitEvidence
from .process import ProcessResult
from .verifier import VerificationResult


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


_FAILURE_STAGES = frozenset({
    "baseline", "attempt-snapshot", "worker", "worker-classification", "git-evidence",
    "attempt-evidence", "scope-assessment", "verification", "verification-assessment",
    "review", "review-validation", "result-or-retry", "findings", "result-construction",
    "task-file", "worker-setup",
})
_FAILURE_TYPES = frozenset(name for name, value in vars(builtins).items()
    if isinstance(value, type) and issubclass(value, BaseException)) | {
    "ValidationError", "CalledProcessError", "TimeoutExpired", "AccessDenied",
    "NoSuchProcess", "ZombieProcess", "HandoffError",
}


def _identity(value):
    """Hash only known text shapes; never serialize arbitrary evidence objects."""
    if type(value) is str:
        raw = value.encode("utf-8")
    elif type(value) is list and all(type(item) is str for item in value):
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
    else:
        return None
    return hashlib.sha256(raw).hexdigest()


def _failure_type(name):
    return name if type(name) is str and name in _FAILURE_TYPES else "other"


def _process_summary(process: ProcessResult) -> dict:
    fields = ("started", "exit_code", "timed_out", "cancelled", "elapsed_seconds",
              "output_complete", "stdout_truncated", "stderr_truncated", "cleanup_attempted")
    result = {name: value for name in fields
              if (value := getattr(process, name)) is None or type(value) in (bool, int, float)}
    result.update(
        infrastructure_failed=process.infrastructure_failed,
        failure_type=_failure_type(process.failure_type) if process.failure_type else None,
        failure_type_sha256=_identity(process.failure_type),
        argv_sha256=_identity(process.argv),
        stdout_sha256=_identity(process.stdout), stderr_sha256=_identity(process.stderr),
        cleanup_error_count=len(process.cleanup_errors),
        cleanup_errors_sha256=_identity(process.cleanup_errors),
        surviving_pid_count=len(process.surviving_pids),
    )
    return result


def _git_summary(evidence) -> dict:
    if isinstance(evidence, (GitBaseline, GitEvidence)):
        get = lambda key: getattr(evidence, key, None)
    elif type(evidence) is dict:
        get = evidence.get
    else:
        return {}
    result = {}
    for key in ("commit", "baseline_commit", "final_head"):
        value = get(key)
        if type(value) is str and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
            result[key] = value
    for key in ("status", "final_status", "patch"):
        if (digest := _identity(get(key))) is not None:
            result[key + "_sha256"] = digest
    for key in ("changed_files", "commits"):
        if type(value := get(key)) is list:
            result[key + "_count"] = len(value)
    return result


def _failure_record_summary(record: dict) -> dict:
    """Project named native observations; unknown fields and text stay private."""
    result = {}
    worker = record.get("worker_result")
    if isinstance(worker, WorkerRunResult):
        result["worker_result"] = {"process": _process_summary(worker.process)}
    if isinstance(process := record.get("failed_process"), ProcessResult):
        result["failed_process"] = _process_summary(process)
    for key in ("baseline", "git_evidence", "partial_git_evidence"):
        if key in record:
            result[key] = _git_summary(record[key])
    verification = record.get("verification")
    if isinstance(verification, VerificationResult):
        result["verification"] = {
            "passed": verification.passed, "completed_check_count": len(verification.checks),
            "checks": [{**_process_summary(check), "id_sha256": _identity(check.id),
                        "passed": check.passed, "expectation_met": check.expectation_met}
                       for check in verification.checks],
        }
    return result


def _print_failure_evidence(exc: BaseException, *, stage: str = "unknown") -> None:
    """Print an allowlisted summary; rich native exception evidence stays intact."""
    try:
        evidence = getattr(exc, "harness_evidence", None)
        evidence = evidence if type(evidence) is dict else {}
        observed_stage = evidence.get("stage", stage)
        stage = observed_stage if type(observed_stage) is str and observed_stage in _FAILURE_STAGES else "unknown"
        attempts = evidence.get("attempts")
        summary = _failure_record_summary(evidence)
        if type(attempts) is list:
            summary["attempts"] = [_failure_record_summary(a) if type(a) is dict else {}
                                   for a in attempts]
            count = len(attempts)
        else:
            count = (None if stage == "unknown" else
                     int(stage not in ("baseline", "task-file", "worker-setup")))
        summary.update(stage=stage, failure_type=_failure_type(type(exc).__name__),
                       failure_type_sha256=_identity(type(exc).__name__), attempt_count=count)
        print("Failure evidence: " + json.dumps(summary), file=sys.stderr)
    except Exception:
        print("Failure evidence unavailable.", file=sys.stderr)


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
            "Repository does not exist.",
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
            "Task file error: details omitted.",
            file=sys.stderr,
        )
        _print_failure_evidence(exc, stage="task-file")
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
        _print_failure_evidence(exc, stage="worker-setup")
        return 130
    except Exception as exc:
        print(
            "Harness run failed.",
            file=sys.stderr,
        )
        _print_failure_evidence(exc, stage="worker-setup")
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
