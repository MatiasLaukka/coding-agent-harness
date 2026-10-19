from __future__ import annotations

import hashlib
import json
import math
import sys
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Sequence

from pydantic import Field, ValidationError

from . import report
from .console import InvalidConsoleLine, ReviewConsole, WindowsConsole
from .control_records import (
    RecordDirectory,
    RecordPublicationError,
    RecordReference,
    prepare_record_directory,
    publish_record,
)
from .git import GitBaseline, capture_baseline
from .human_review import HumanReviewer, _terminal_json, render_start_authorization
from .models import StrictModel, TaskPacket, VerificationCommand
from .worker import QwenWorker
from .controller import HarnessController

MAX_TASK_FILE_BYTES = 256 * 1024
MAX_TASK_GOAL_CHARS = 2900
MAX_TASK_ID_CHARS = 128
MAX_TASK_LIST_ITEMS = 64
MAX_FILE_SCOPE_ITEMS = 256
MAX_PATH_CHARS = 1024
MAX_TEXT_ITEM_CHARS = 2000
MAX_TOTAL_TASK_TEXT_CHARS = 16_000
MAX_VERIFICATION_CHECKS = 8
MAX_ARGV_ITEMS = 64
MAX_ARG_CHARS = 4096
MAX_TOTAL_ARG_CHARS = 32_768
MAX_VERIFICATION_TIMEOUT_SECONDS = 1800
MAX_WORKER_TIMEOUT_SECONDS = 14_400
MAX_REVIEW_TIMEOUT_SECONDS = 1800


class ControlTaskFile(StrictModel):
    task: TaskPacket
    verification_commands: list[VerificationCommand] = Field(default_factory=list)


class ControlAdmissionError(ValueError):
    pass


class _WorkflowState:
    def __init__(self) -> None:
        self.workflow_id = uuid.uuid4().hex
        self.stage = "preflight"
        self.record_directory: RecordDirectory | None = None
        self.start_record: RecordReference | None = None
        self.reviewer: HumanReviewer | None = None
        self.result = None
        self.error: BaseException | None = None
        self.interrupted = False
        self.controller_called = False
        self.terminal_publish_attempted = False
        self.terminal_published = False
        self.console: ReviewConsole | None = None
        self.repo_dir: Path | None = None
        self.task_file: Path | None = None
        self.task: TaskPacket | None = None
        self.commands: list[VerificationCommand] = []
        self.execution_configuration: dict[str, Any] = {}
        self.configuration_sha256: str | None = None
        self.baseline: GitBaseline | None = None


def _duplicate_checked_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ControlAdmissionError("task file contains a duplicate JSON key")
        result[key] = value
    return result


def _load_control_task_file(path: Path) -> ControlTaskFile:
    with path.open("rb") as stream:
        raw = stream.read(MAX_TASK_FILE_BYTES + 1)
    if len(raw) > MAX_TASK_FILE_BYTES:
        raise ControlAdmissionError("task file exceeds its size bound")
    try:
        text = raw.decode("utf-8", errors="strict")
        payload = json.loads(text, object_pairs_hook=_duplicate_checked_object)
        spec = ControlTaskFile.model_validate(payload, strict=True)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
        raise ControlAdmissionError("task file is invalid") from error
    _validate_task_and_checks(spec)
    return spec


def _bounded_text(value: str, *, label: str, maximum: int, allow_blank: bool = False) -> None:
    if type(value) is not str or len(value) > maximum or "\0" in value:
        raise ControlAdmissionError(f"{label} exceeds its bound")
    if not allow_blank and not value.strip():
        raise ControlAdmissionError(f"{label} must not be blank")


def _validate_allowed_path(value: str) -> None:
    _bounded_text(value, label="allowed file path", maximum=MAX_PATH_CHARS)
    path = PurePosixPath(value)
    if (
        "\\" in value or ":" in value or path.is_absolute()
        or path.as_posix() != value
        or any(part in ("", ".", "..") for part in value.split("/"))
        or any(part.casefold() == ".git" for part in path.parts)
    ):
        raise ControlAdmissionError("allowed files must be canonical repository-relative paths")


def _validate_task_and_checks(spec: ControlTaskFile) -> None:
    task = spec.task
    _bounded_text(task.task_id, label="task id", maximum=MAX_TASK_ID_CHARS)
    _bounded_text(task.goal, label="task goal", maximum=MAX_TASK_GOAL_CHARS)
    if not task.acceptance or len(task.acceptance) > MAX_TASK_LIST_ITEMS:
        raise ControlAdmissionError("acceptance criteria count is out of range")
    if len(task.constraints) > MAX_TASK_LIST_ITEMS:
        raise ControlAdmissionError("constraint count is out of range")
    if len(task.suggested_files) > MAX_FILE_SCOPE_ITEMS:
        raise ControlAdmissionError("suggested file count is out of range")
    if task.allowed_files is None or not 1 <= len(task.allowed_files) <= MAX_FILE_SCOPE_ITEMS:
        raise ControlAdmissionError("one or more explicit allowed files are required")

    text_items = [*task.constraints, *task.acceptance]
    for value in text_items:
        _bounded_text(value, label="task text", maximum=MAX_TEXT_ITEM_CHARS)
    if sum(len(value) for value in text_items) + len(task.goal) > MAX_TOTAL_TASK_TEXT_CHARS:
        raise ControlAdmissionError("task text exceeds its aggregate bound")
    for value in [*task.suggested_files, *task.allowed_files]:
        _bounded_text(value, label="task path", maximum=MAX_PATH_CHARS)
    for value in task.allowed_files:
        _validate_allowed_path(value)
    if len({value.casefold() for value in task.allowed_files}) != len(task.allowed_files):
        raise ControlAdmissionError("allowed file paths must be unique")

    commands = spec.verification_commands
    if not 1 <= len(commands) <= MAX_VERIFICATION_CHECKS:
        raise ControlAdmissionError("one to eight verification checks are required")
    if not any(command.expectation != "advisory" for command in commands):
        raise ControlAdmissionError("at least one required verification check is required")
    identifiers = [command.id.casefold() for command in commands]
    if len(set(identifiers)) != len(identifiers):
        raise ControlAdmissionError("verification check ids must be unique")
    aggregate_args = 0
    for command in commands:
        _bounded_text(command.id, label="verification id", maximum=64)
        if not 1 <= command.timeout_seconds <= MAX_VERIFICATION_TIMEOUT_SECONDS:
            raise ControlAdmissionError("verification timeout is out of range")
        if not 1 <= len(command.argv) <= MAX_ARGV_ITEMS:
            raise ControlAdmissionError("verification argument count is out of range")
        for argument in command.argv:
            _bounded_text(argument, label="verification argument", maximum=MAX_ARG_CHARS)
            aggregate_args += len(argument)
        if command.expectation_reason is not None:
            _bounded_text(command.expectation_reason, label="verification reason", maximum=MAX_TEXT_ITEM_CHARS)
    if aggregate_args > MAX_TOTAL_ARG_CHARS:
        raise ControlAdmissionError("verification arguments exceed their aggregate bound")


def _validate_execution_options(args) -> tuple[str, str, float, int, int]:
    model = args.model
    command = args.worker_command
    worker_timeout = args.worker_timeout
    review_timeout = args.review_timeout
    remediation_budget = args.max_remediation_attempts
    _bounded_text(model, label="model", maximum=256)
    _bounded_text(command, label="worker command", maximum=1024)
    if type(worker_timeout) not in (int, float) or not math.isfinite(worker_timeout):
        raise ControlAdmissionError("worker timeout must be finite")
    if not 1 <= worker_timeout <= MAX_WORKER_TIMEOUT_SECONDS:
        raise ControlAdmissionError("worker timeout is out of range")
    if type(review_timeout) is not int or not 1 <= review_timeout <= MAX_REVIEW_TIMEOUT_SECONDS:
        raise ControlAdmissionError("review timeout is out of range")
    if type(remediation_budget) is not int or remediation_budget not in (0, 1):
        raise ControlAdmissionError("max remediation attempts must be 0 or 1")
    return model, command, float(worker_timeout), review_timeout, remediation_budget


def _canonical_digest(value: dict[str, Any]) -> str:
    content = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8", errors="strict")
    return hashlib.sha256(content).hexdigest()


def _capture_start_authorization(
    *, console: ReviewConsole, challenge: str, timeout_seconds: int,
    clock: Callable[[], float],
) -> float:
    now = clock()
    deadline = now + timeout_seconds
    if not math.isfinite(now) or not math.isfinite(deadline) or deadline <= now:
        raise ControlAdmissionError("start authorization deadline is invalid")
    expected = f"START {challenge}"
    console.discard_typeahead()
    while True:
        if clock() >= deadline:
            raise ConsoleDeadlineExpired("start authorization deadline expired")
        try:
            line = console.read_line(deadline=deadline)
        except InvalidConsoleLine:
            console.write("Input rejected by the bounded console reader.\n")
            continue
        if clock() >= deadline:
            raise ConsoleDeadlineExpired("start authorization deadline expired")
        if line == expected:
            return deadline
        console.write("Authorization text did not match. Enter the displayed START challenge.\n")


def _invocation_observation(state: _WorkflowState) -> dict[str, Any]:
    if state.result is not None:
        return {
            "count": state.result.attempts,
            "basis": "returned HarnessController attempts; one worker dispatch per attempt",
        }
    if not state.controller_called:
        return {"count": 0, "basis": "controller was not called"}
    evidence = getattr(state.error, "harness_evidence", None)
    if type(evidence) is dict:
        count = evidence.get("worker_invocation_count")
        if type(count) is int and 0 <= count <= 2:
            return {"count": count, "basis": "HarnessController native exception evidence"}
    return {"count": None, "basis": "unavailable"}


def _review_references(state: _WorkflowState) -> list[dict[str, Any]]:
    if state.reviewer is None:
        return []
    returned = set(state.reviewer.callback_returned_record_ids)
    return [
        {**reference.model_dump(), "controller_callback_returned": reference.record_id in returned}
        for reference in state.reviewer.review_records
    ]


def _terminal_payload(state: _WorkflowState) -> dict[str, Any]:
    error = state.error
    harness_evidence = getattr(error, "harness_evidence", None) if error is not None else None
    native = {}
    if type(harness_evidence) is dict:
        native["harness_evidence"] = harness_evidence
    if error is not None:
        for name in ("process_result", "git_failure", "git_cleanup_failure"):
            value = getattr(error, name, None)
            if value is not None:
                native[name] = value
        publication_reference = getattr(error, "reference", None)
        if publication_reference is not None:
            native["record_publication"] = {
                **publication_reference.model_dump(),
                "published": bool(getattr(error, "published", False)),
            }
    controller_context = harness_evidence if type(harness_evidence) is dict else {}
    controller_stage = controller_context.get("stage")
    if type(controller_stage) is not str:
        controller_stage = None
    payload: dict[str, Any] = {
        "workflow_id": state.workflow_id,
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "workflow_stage": state.stage,
        "workflow_interrupted": state.interrupted,
        "start_record": state.start_record.model_dump() if state.start_record else None,
        "review_records": _review_references(state),
        "human_requested_acceptance_is_not_controller_acceptance": True,
        "worker_invocation_observation": _invocation_observation(state),
        "terminal_record_required": True,
        "record_notice": (
            "Records may contain sensitive unredacted data; they are not tamper-proof, "
            "authenticated, or crash-durable. A missing terminal record means the workflow is incomplete."
        ),
        "saved_records_are_not_authorization_or_resume_checkpoints": True,
    }
    if state.result is not None:
        payload["controller_result"] = state.result.model_dump(mode="json", warnings=False)
        payload["controller_result_source"] = "HarnessController.run return value"
    else:
        payload["controller_result"] = None
    if error is not None:
        payload["workflow_failure"] = {
            "stage": state.stage,
            "controller_stage": controller_stage,
            "exception_type": type(error).__name__,
            "review_record_failure": getattr(error, "review_record_failure", None),
            "console_restore_error": getattr(error, "console_restore_error", None),
            "record_published_before_failure": getattr(error, "record_published", None),
        }
    else:
        payload["workflow_failure"] = None
    payload["native_evidence"] = native
    return payload


def _write_notice(console: ReviewConsole | None, payload: dict[str, Any]) -> None:
    text = _terminal_json(payload) + "\n"
    if console is not None:
        console.write(text)
    else:
        sys.stderr.write(text)


def _finalize(state: _WorkflowState, console: ReviewConsole | None) -> int:
    if state.terminal_publish_attempted:
        if state.interrupted:
            _write_notice(console, {
                "event": "control_interrupted",
                "terminal_record_published": state.terminal_published,
                "controller_status": getattr(state.result, "status", None),
            })
            return 130
        if state.terminal_published:
            _write_notice(console, {
                "event": "workflow_error_after_terminal_publication",
                "controller_status": getattr(state.result, "status", None),
                "terminal_record_published": True,
            })
        elif state.result is not None:
            _write_notice(console, {
                "event": "terminal_record_unavailable",
                "controller_status": state.result.status,
            })
        return 1

    if state.record_directory is None:
        _write_notice(console, {
            "event": "control_workflow_incomplete",
            "stage": state.stage,
            "reason": "trusted record directory could not be established; no worker was dispatched",
            "interrupted": state.interrupted,
        })
        return 130 if state.interrupted else 1

    state.terminal_publish_attempted = True
    try:
        reference = publish_record(
            state.record_directory, kind="terminal", record=_terminal_payload(state),
        )
        state.terminal_published = True
    except KeyboardInterrupt as error:
        state.error = state.error or error
        state.interrupted = True
        _write_notice(console, {
            "event": "terminal_publication_interrupted",
            "controller_status": getattr(state.result, "status", None),
            "worker_invocation_observation": _invocation_observation(state),
        })
        return 130
    except Exception as error:
        if state.error is None:
            state.error = error
        status = getattr(state.result, "status", None)
        _write_notice(console, {
            "event": "terminal_publication_failed",
            "controller_status": status,
            "worker_invocation_observation": _invocation_observation(state),
            "publication_error_type": type(error).__name__,
            "actual_controller_outcome_preserved_in_native_evidence": state.result is not None,
        })
        return 130 if state.interrupted else 1

    status = getattr(state.result, "status", None)
    notice = {
        "event": "terminal_record_published",
        "record": reference.model_dump(),
        "controller_status": status,
        "worker_invocation_observation": _invocation_observation(state),
        "workflow_interrupted": state.interrupted,
    }
    try:
        _write_notice(console, notice)
    except Exception:
        return 130 if state.interrupted else 1
    if state.interrupted:
        return 130
    if state.error is not None:
        return 1
    if state.result is None:
        return 1
    return 0 if state.result.status == "accepted" else 2


def _context_for_console(value):
    if hasattr(value, "__enter__") and hasattr(value, "__exit__"):
        return value
    return nullcontext(value)


def run_control(
    args,
    *,
    console_factory: Callable[[], ReviewConsole] = WindowsConsole.open,
    worker_factory=QwenWorker,
    controller_factory=HarnessController,
    clock: Callable[[], float] = time.monotonic,
    challenge_factory: Callable[[], str] | None = None,
) -> int:
    """Execute one explicitly authorized, human-reviewed controller workflow."""
    state = _WorkflowState()
    try:
        state.stage = "repository-preflight"
        repo_dir = report._canonical_directory(Path(args.repo))
        git_roots = report.discover_report_roots(repo_dir)
        state.repo_dir = repo_dir

        state.stage = "record-directory-preflight"
        state.record_directory = prepare_record_directory(
            Path(args.record_dir), repo_dir=repo_dir, git_roots=git_roots,
        )

        state.stage = "task-file"
        task_file = Path(args.task_file).resolve(strict=True)
        spec = _load_control_task_file(task_file)
        state.task_file = task_file
        state.task = spec.task
        state.commands = spec.verification_commands

        state.stage = "execution-configuration"
        model, worker_command, worker_timeout, review_timeout, budget = _validate_execution_options(args)
        worker = worker_factory(
            command=[worker_command], model=model, timeout_seconds=worker_timeout,
            stdin_devnull=True,
        )
        first_argv = worker.command_for(spec.task)
        execution_config = {
            "task_file": str(task_file),
            "task": spec.task.model_dump(mode="json", warnings=False),
            "verification_commands": [
                command.model_dump(mode="json", warnings=False) for command in spec.verification_commands
            ],
            "model": model,
            "worker_command": [worker_command],
            "first_dispatch_argv": first_argv,
            "worker_timeout_seconds": worker_timeout,
            "review_timeout_seconds": review_timeout,
            "max_remediation_attempts": budget,
            "stdin_policy": "DEVNULL for worker and verification subprocesses",
            "canonical_worktree_root": str(repo_dir),
            "git_metadata_roots": [str(root) for root in git_roots],
            "record_only_directory": str(state.record_directory.directory),
        }
        state.execution_configuration = execution_config
        state.configuration_sha256 = _canonical_digest(execution_config)

        state.stage = "console-validation"
        console_context = _context_for_console(console_factory())
        with console_context as console:
            state.console = console
            try:
                state.stage = "approved-baseline"
                baseline = capture_baseline(repo_dir)
                state.baseline = baseline

                start_challenge = (challenge_factory or (lambda: uuid.uuid4().hex[:8].upper()))()
                if type(start_challenge) is not str or not len(start_challenge) == 8 or not all(
                    char in "0123456789ABCDEF" for char in start_challenge
                ):
                    raise ControlAdmissionError("start challenge generation failed")
                state.stage = "start-authorization"
                console.write(render_start_authorization(
                    task=spec.task,
                    verification_commands=spec.verification_commands,
                    model=model,
                    worker_command=[worker_command],
                    launcher_argv=first_argv,
                    worker_timeout=worker_timeout,
                    review_timeout=review_timeout,
                    max_remediation_attempts=budget,
                    repository_root=str(repo_dir),
                    git_roots=[str(root) for root in git_roots],
                    record_directory=str(state.record_directory.directory),
                    baseline=baseline,
                    config_sha256=state.configuration_sha256,
                    challenge=start_challenge,
                ))
                start_deadline = _capture_start_authorization(
                    console=console, challenge=start_challenge,
                    timeout_seconds=review_timeout, clock=clock,
                )

                state.stage = "start-record-publication"
                start_record = {
                    "workflow_id": state.workflow_id,
                    "created_at_utc": datetime.now(timezone.utc).isoformat(),
                    "execution_configuration_sha256": state.configuration_sha256,
                    "frozen_task": spec.task.model_dump(mode="json", warnings=False),
                    "frozen_verification_commands": [
                        command.model_dump(mode="json", warnings=False)
                        for command in spec.verification_commands
                    ],
                    "execution_configuration": execution_config,
                    "approved_clean_baseline": baseline.model_dump(mode="json", warnings=False),
                    "initial_authorization": {
                        "action": "START",
                        "accepted": True,
                        "challenge_sha256": hashlib.sha256(start_challenge.encode("ascii")).hexdigest(),
                        "scope": "exactly the first worker dispatch under the displayed configuration",
                    },
                    "remediation_budget": {"maximum": budget, "used": 0},
                    "terminal_record_required": True,
                    "record_notice": (
                        "Records may contain sensitive unredacted data; they are not tamper-proof, "
                        "authenticated, or crash-durable. The operator is responsible for keeping "
                        "the existing record-only directory independent of operational resources."
                    ),
                    "saved_records_are_not_authorization_or_resume_checkpoints": True,
                }
                state.start_record = publish_record(
                    state.record_directory, kind="start", record=start_record,
                )
                if clock() >= start_deadline:
                    raise ConsoleDeadlineExpired("start authorization deadline expired")

                state.reviewer = HumanReviewer(
                    console=console,
                    repo_dir=repo_dir,
                    baseline=baseline,
                    verification_commands=spec.verification_commands,
                    review_timeout=review_timeout,
                    max_remediation_attempts=budget,
                    record_directory=state.record_directory,
                    workflow_id=state.workflow_id,
                    start_record=state.start_record,
                    config_sha256=state.configuration_sha256,
                    challenge_factory=challenge_factory,
                    clock=clock,
                )
                controller = controller_factory(
                    worker=worker, reviewer=state.reviewer,
                    max_remediation_attempts=budget, stdin_devnull=True,
                )
                state.stage = "controller"
                state.controller_called = True
                state.result = controller.run(
                    task=spec.task,
                    repo_dir=repo_dir,
                    verification_commands=spec.verification_commands,
                    expected_baseline=baseline,
                )
            except KeyboardInterrupt as error:
                state.error = error
                state.interrupted = True
            except Exception as error:
                state.error = error
            return _finalize(state, console)
    except KeyboardInterrupt as error:
        state.error = state.error or error
        state.interrupted = True
        return _finalize(state, None)
    except Exception as error:
        state.error = state.error or error
        return _finalize(state, None)
