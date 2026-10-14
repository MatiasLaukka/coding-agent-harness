from __future__ import annotations

import math
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from coding_agent_harness.models import StrictModel
from coding_agent_harness.process import (
    PROCESS_CLEANUP_RESERVE_SECONDS,
    ProcessResult,
    run_process,
)


# Total monotonic budgets for each public Git evidence phase.
BASELINE_TIMEOUT_SECONDS = 15
SNAPSHOT_TIMEOUT_SECONDS = 30
CUMULATIVE_TIMEOUT_SECONDS = 45
ATTEMPT_EVIDENCE_TIMEOUT_SECONDS = 30

# Per-command capture limits. The patch remains textual Git --binary output.
GIT_STDOUT_LIMIT_BYTES = 1024 * 1024
GIT_PATCH_STDOUT_LIMIT_BYTES = 8 * 1024 * 1024
GIT_STDERR_LIMIT_BYTES = 64 * 1024

_INDEX_COPY_CHUNK_BYTES = 64 * 1024
_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")

_GIT_FAILURE_REASONS = frozenset({
    "deadline-exceeded",
    "output-budget-exceeded",
    "incomplete-collection",
    "git-launch-failed",
    "git-nonzero-exit",
    "cleanup-failed",
    "termination-unconfirmed",
    "baseline-not-clean",
})

_GIT_FAILURE_STAGES = frozenset({
    "baseline-head",
    "baseline-status",
    "baseline-cleanliness",
    "snapshot-index-init",
    "snapshot-read-tree",
    "snapshot-add",
    "snapshot-write-tree",
    "snapshot-cleanup",
    "cumulative-head",
    "cumulative-status",
    "cumulative-tracked-files",
    "cumulative-untracked-files",
    "cumulative-patch-index-path",
    "cumulative-patch-index-init",
    "cumulative-patch-index-copy",
    "cumulative-patch-index-add",
    "cumulative-patch-index-cleanup",
    "cumulative-patch",
    "cumulative-commits",
    "attempt-after-snapshot-index-init",
    "attempt-after-snapshot-read-tree",
    "attempt-after-snapshot-add",
    "attempt-after-snapshot-write-tree",
    "attempt-after-snapshot-cleanup",
    "attempt-diff",
})


@dataclass(frozen=True)
class GitFailureMetadata:
    """Safe, structured failure fields; never includes raw Git output or paths."""

    reason: str
    stage: str
    exit_code: int | None = None
    stdout_limit_bytes: int = GIT_STDOUT_LIMIT_BYTES
    stderr_limit_bytes: int = GIT_STDERR_LIMIT_BYTES
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    cleanup_error_count: int = 0
    surviving_process_count: int = 0
    filesystem_operation_pending_count: int = 0
    temporary_artifact_unresolved_count: int = 0
    temporary_artifact_cleanup_uncertain: bool = False


class GitCollectionError(RuntimeError):
    def __init__(
        self,
        failure: GitFailureMetadata,
        *,
        process_result: ProcessResult | None = None,
    ):
        self.git_failure = failure
        self.reason = failure.reason
        self.stage = failure.stage
        if process_result is not None:
            self.process_result = process_result
        message = (
            "working tree must be clean before starting a delegated task"
            if failure.reason == "baseline-not-clean"
            else f"Git evidence collection failed: {failure.reason} at {failure.stage}"
        )
        super().__init__(message)


class GitBaseline(StrictModel):
    commit: str
    status: str


class GitEvidence(StrictModel):
    baseline_commit: str
    final_head: str
    final_status: str
    changed_files: list[str]
    patch: str
    commits: list[str]


class GitWorktreeSnapshot(StrictModel):
    tree: str


class GitAttemptEvidence(StrictModel):
    before_tree: str
    after_tree: str
    changed_files: list[str]


def _failure_metadata(
    reason: str,
    stage: str,
    *,
    process_result: ProcessResult | None = None,
    stdout_limit_bytes: int = GIT_STDOUT_LIMIT_BYTES,
    stderr_limit_bytes: int = GIT_STDERR_LIMIT_BYTES,
    cleanup_error_count: int | None = None,
    filesystem_operation_pending_count: int = 0,
    temporary_artifact_unresolved_count: int = 0,
    temporary_artifact_cleanup_uncertain: bool = False,
) -> GitFailureMetadata:
    return GitFailureMetadata(
        reason=reason,
        stage=stage if stage in _GIT_FAILURE_STAGES else "incomplete-collection",
        exit_code=process_result.exit_code if process_result is not None else None,
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=stderr_limit_bytes,
        stdout_truncated=process_result.stdout_truncated if process_result is not None else False,
        stderr_truncated=process_result.stderr_truncated if process_result is not None else False,
        cleanup_error_count=(
            cleanup_error_count
            if cleanup_error_count is not None
            else len(process_result.cleanup_errors) if process_result is not None else 0
        ),
        surviving_process_count=len(process_result.surviving_pids) if process_result is not None else 0,
        filesystem_operation_pending_count=filesystem_operation_pending_count,
        temporary_artifact_unresolved_count=temporary_artifact_unresolved_count,
        temporary_artifact_cleanup_uncertain=temporary_artifact_cleanup_uncertain,
    )


def _attach_failure(
    exc: BaseException,
    failure: GitFailureMetadata,
    *,
    attribute: str = "git_failure",
) -> None:
    if not hasattr(exc, attribute):
        setattr(exc, attribute, failure)


def _attach_observations(exc: BaseException, observations: dict) -> None:
    exc.git_evidence = observations


def _raise_git_failure(
    reason: str,
    stage: str,
    *,
    process_result: ProcessResult | None = None,
    stdout_limit_bytes: int = GIT_STDOUT_LIMIT_BYTES,
    stderr_limit_bytes: int = GIT_STDERR_LIMIT_BYTES,
) -> None:
    failure = _failure_metadata(
        reason,
        stage,
        process_result=process_result,
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=stderr_limit_bytes,
    )
    raise GitCollectionError(failure, process_result=process_result)


def _new_deadline(timeout_seconds: float) -> float:
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("Git collection timeout must be finite and > 0")
    started = time.monotonic()
    deadline = started + timeout_seconds
    if not math.isfinite(deadline) or deadline <= started:
        raise ValueError("Git collection timeout produced an invalid deadline")
    return deadline


def _remaining(deadline: float, stage: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _raise_git_failure("deadline-exceeded", stage)
    return remaining


def _process_failure_reason(result: ProcessResult, deadline: float) -> str | None:
    # Cleanup uncertainty takes precedence because it means the subprocess
    # collection could not establish a complete, safe observation.
    if result.cleanup_errors:
        return "cleanup-failed"
    if result.surviving_pids:
        return "termination-unconfirmed"
    if result.timed_out or time.monotonic() >= deadline:
        return "deadline-exceeded"
    if result.stdout_truncated or result.stderr_truncated:
        return "output-budget-exceeded"
    if not result.started:
        return "git-launch-failed"
    if not result.output_complete or result.exit_code is None:
        return "incomplete-collection"
    if result.exit_code != 0:
        return "git-nonzero-exit"
    return None


def _filesystem_failure(
    reason: str,
    stage: str,
    *,
    pending: int = 0,
    unresolved: int = 0,
    cleanup_uncertain: bool = False,
) -> GitCollectionError:
    return GitCollectionError(
        _failure_metadata(
            reason,
            stage,
            filesystem_operation_pending_count=pending,
            temporary_artifact_unresolved_count=unresolved,
            temporary_artifact_cleanup_uncertain=cleanup_uncertain,
        )
    )


def _bounded_filesystem_call(
    operation,
    *,
    deadline: float,
    stage: str,
    pending_artifacts: int = 0,
    cleanup: bool = False,
    on_starting=None,
    on_late_completion=None,
):
    """Run one blocking filesystem phase without extending its absolute deadline.

    Python cannot cancel a blocked filesystem syscall. The daemon helper retains
    ownership until it returns; if the caller abandons it, late cleanup runs only
    after the syscall has completed. The caller receives explicit uncertainty.
    """
    _remaining(deadline, stage)
    wait_deadline = deadline if cleanup else deadline - PROCESS_CLEANUP_RESERVE_SECONDS
    if wait_deadline <= time.monotonic():
        _raise_git_failure("deadline-exceeded", stage)

    state_lock = threading.Lock()
    completed = threading.Event()
    decision = threading.Event()
    state = {
        "value": None,
        "error": None,
        "traceback": None,
        "completed_at": None,
        "abandoned": False,
        "lifecycle": "not-started",
    }

    def run_operation() -> None:
        try:
            value = operation()
            error = None
            error_traceback = None
        except BaseException as exc:
            value = None
            error = exc
            error_traceback = exc.__traceback__
        with state_lock:
            state["value"] = value
            state["error"] = error
            state["traceback"] = error_traceback
            state["completed_at"] = time.monotonic()
            state["lifecycle"] = "completed"
        completed.set()
        decision.wait()
        with state_lock:
            abandoned = state["abandoned"]
        if abandoned and on_late_completion is not None:
            try:
                on_late_completion(value, error)
            except BaseException:
                # The caller already failed closed and records unresolved
                # artifacts. A late best-effort cleanup cannot revise it.
                pass
            finally:
                with state_lock:
                    state["lifecycle"] = "late-cleanup-completed"

    helper = threading.Thread(
        target=run_operation,
        daemon=True,
        name="harness-git-filesystem",
    )
    try:
        # Record possible helper ownership before startup: Thread.start() can
        # raise in the caller after the new thread has become live.
        with state_lock:
            state["lifecycle"] = "may-be-running"
        if on_starting is not None:
            on_starting()
        helper.start()
        completed.wait(max(0.0, wait_deadline - time.monotonic()))
        with state_lock:
            completed_at = state["completed_at"]
            value = state["value"]
            error = state["error"]
            error_traceback = state["traceback"]
            pending = completed_at is None
        late = not pending and (
            completed_at >= wait_deadline or time.monotonic() >= deadline
        )
        if pending or late:
            with state_lock:
                state["abandoned"] = True
                state["lifecycle"] = "abandoned"
            failure = _filesystem_failure(
                "deadline-exceeded",
                stage,
                pending=1,
                unresolved=pending_artifacts,
                cleanup_uncertain=bool(pending_artifacts),
            )
            failure.git_filesystem_operation_pending = True
            decision.set()
            raise failure
        if error is not None:
            decision.set()
            raise error.with_traceback(error_traceback)
        # Recheck after completion was observed, while the helper still waits
        # for its decision and can safely perform late cleanup.
        _remaining(deadline, stage)
        with state_lock:
            state["lifecycle"] = "accepted"
        decision.set()
        return value
    except BaseException as exc:
        # Deliberate failures above release the helper before raising. If an
        # interrupt lands after that handoff, the filesystem call is already
        # complete and the caller's enclosing owner handles cleanup.
        if decision.is_set():
            raise
        with state_lock:
            state["abandoned"] = True
            state["lifecycle"] = "abandoned"
        decision.set()
        existing_failure = getattr(exc, "git_failure", None)
        reason = (
            "deadline-exceeded"
            if isinstance(existing_failure, GitFailureMetadata)
            and existing_failure.reason == "deadline-exceeded"
            else "incomplete-collection"
        )
        _attach_failure(
            exc,
            _failure_metadata(
                reason,
                stage,
                filesystem_operation_pending_count=1,
                temporary_artifact_unresolved_count=pending_artifacts,
                temporary_artifact_cleanup_uncertain=bool(pending_artifacts),
            ),
        )
        exc.git_filesystem_operation_pending = True
        raise


def _termination_confirmed(result: ProcessResult) -> bool:
    return bool(
        not result.started
        or (
            result.exit_code is not None
            and not result.cleanup_errors
            and not result.surviving_pids
        )
    )


def _git(
    repo: Path,
    *args: str,
    deadline: float,
    stage: str,
    env: dict[str, str] | None = None,
    stdout_limit_bytes: int = GIT_STDOUT_LIMIT_BYTES,
    temporary_index: _TemporaryIndex | None = None,
) -> str:
    remaining = _remaining(deadline, stage)
    if temporary_index is not None:
        temporary_index.process_may_be_active = True
        temporary_index.process_result = None
    try:
        result = run_process(
            argv=["git", *args],
            cwd=repo,
            env=env,
            timeout_seconds=remaining,
            deadline=deadline,
            stdout_limit_bytes=stdout_limit_bytes,
            stderr_limit_bytes=GIT_STDERR_LIMIT_BYTES,
        )
    except BaseException as exc:
        process_result = getattr(exc, "process_result", None)
        if temporary_index is not None:
            if isinstance(process_result, ProcessResult):
                temporary_index.process_result = process_result
                temporary_index.process_may_be_active = not _termination_confirmed(process_result)
            # With no result, a child may have been launched. Keep the exact
            # temporary index and lock until termination can be confirmed.
        if isinstance(process_result, ProcessResult):
            reason = _process_failure_reason(process_result, deadline) or "incomplete-collection"
        else:
            reason = "incomplete-collection"
        _attach_failure(
            exc,
            _failure_metadata(
                reason,
                stage,
                process_result=process_result if isinstance(process_result, ProcessResult) else None,
                stdout_limit_bytes=stdout_limit_bytes,
            ),
        )
        raise

    if temporary_index is not None:
        if isinstance(result, ProcessResult):
            temporary_index.process_result = result
            temporary_index.process_may_be_active = not _termination_confirmed(result)
        else:
            temporary_index.process_may_be_active = True
    if not isinstance(result, ProcessResult):
        _raise_git_failure(
            "incomplete-collection",
            stage,
            stdout_limit_bytes=stdout_limit_bytes,
        )
    reason = _process_failure_reason(result, deadline)
    if reason is not None:
        _raise_git_failure(
            reason,
            stage,
            process_result=result,
            stdout_limit_bytes=stdout_limit_bytes,
        )
    return result.stdout

def _require_object_id(output: str, stage: str) -> str:
    value = output.strip()
    if not _OBJECT_ID_RE.fullmatch(value):
        _raise_git_failure("incomplete-collection", stage)
    return value


def _status(repo: Path, *, deadline: float, stage: str) -> str:
    return _git(
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        deadline=deadline,
        stage=stage,
    ).rstrip("\r\n")


def _git_z(
    repo: Path,
    *args: str,
    deadline: float,
    stage: str,
) -> list[str]:
    output = _git(repo, *args, deadline=deadline, stage=stage)
    if output and not output.endswith("\0"):
        _raise_git_failure("incomplete-collection", stage)
    paths = [item for item in output.split("\0") if item]
    _remaining(deadline, stage)
    return paths


def _attach_unexpected_failure(
    exc: BaseException,
    observations: dict,
    stage: str,
) -> None:
    _attach_observations(exc, observations)
    if not hasattr(exc, "git_failure"):
        _attach_failure(exc, _failure_metadata("incomplete-collection", stage))


def capture_baseline(repo: Path) -> GitBaseline:
    observations: dict = {}
    stage = "baseline-head"
    try:
        deadline = _new_deadline(BASELINE_TIMEOUT_SECONDS)
        commit = _require_object_id(
            _git(repo, "rev-parse", "HEAD", deadline=deadline, stage=stage),
            stage,
        )
        observations["commit"] = commit

        stage = "baseline-status"
        status = _status(repo, deadline=deadline, stage=stage)
        observations["status"] = status

        if status:
            stage = "baseline-cleanliness"
            _raise_git_failure(stage="baseline-cleanliness", reason="baseline-not-clean")

        return GitBaseline(commit=commit, status=status)
    except BaseException as exc:
        _attach_unexpected_failure(exc, observations, stage)
        raise


@dataclass
class _TemporaryIndex:
    # artifact_state moves through not-created, may-exist, owned,
    # cleanup-confirmed, or cleanup-uncertain. Keep the raw creation result
    # before deriving paths so an interrupted handoff cannot lose the fd/name.
    path: Path | None = None
    lock_path: Path | None = None
    descriptor: int | None = None
    descriptor_uncertain: bool = False
    creation_result: tuple[int, str] | None = None
    artifact_state: str = "not-created"
    operation_state: str = "not-started"
    cleanup_state: str = "not-started"
    process_may_be_active: bool = False
    process_result: ProcessResult | None = None


def _owned_artifact_count(temporary_index: _TemporaryIndex) -> int:
    path_count = (
        0
        if temporary_index.artifact_state in {"not-created", "cleanup-confirmed"}
        else 2
    )
    descriptor_count = int(
        temporary_index.descriptor is not None or temporary_index.descriptor_uncertain
    )
    return path_count + descriptor_count


def _close_temporary_index_descriptor(temporary_index: _TemporaryIndex) -> None:
    descriptor = temporary_index.descriptor
    if descriptor is None:
        return
    try:
        os.close(descriptor)
    except BaseException:
        # A failed close may still have consumed the handle. Do not retry its
        # numeric value, which the OS could already have reused.
        temporary_index.descriptor = None
        temporary_index.descriptor_uncertain = True
        raise
    else:
        temporary_index.descriptor = None


def _remove_owned_artifacts(
    temporary_index: _TemporaryIndex,
) -> tuple[int, int, BaseException | None]:
    """Best-effort removal of only this collector's index and its exact lock."""
    errors = 0
    first_error: BaseException | None = None
    if temporary_index.descriptor is not None:
        try:
            os.close(temporary_index.descriptor)
        except BaseException as exc:
            errors += 1
            first_error = first_error or exc
            temporary_index.descriptor = None
            temporary_index.descriptor_uncertain = True
        else:
            temporary_index.descriptor = None

    path = temporary_index.path
    lock_path = temporary_index.lock_path
    if (path is None or lock_path is None) and temporary_index.creation_result is not None:
        _fd, path_text = temporary_index.creation_result
        path = Path(path_text)
        lock_path = Path(str(path) + ".lock")
    if path is None or lock_path is None:
        return errors, 2 + int(temporary_index.descriptor_uncertain), first_error

    paths = (lock_path, path)
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        except BaseException as exc:
            errors += 1
            first_error = first_error or exc

    unresolved = 0
    for path in paths:
        try:
            path.stat()
        except FileNotFoundError:
            continue
        except BaseException as exc:
            errors += 1
            unresolved += 1
            first_error = first_error or exc
        else:
            unresolved += 1
    unresolved += int(temporary_index.descriptor_uncertain)
    return errors, unresolved, first_error


def _cleanup_temporary_index(
    temporary_index: _TemporaryIndex,
    *,
    deadline: float,
    stage: str,
) -> GitFailureMetadata | None:
    if temporary_index.process_may_be_active:
        temporary_index.artifact_state = "cleanup-uncertain"
        temporary_index.cleanup_state = "cleanup-uncertain"
        return _failure_metadata(
            "termination-unconfirmed",
            stage,
            process_result=temporary_index.process_result,
            temporary_artifact_unresolved_count=2,
            temporary_artifact_cleanup_uncertain=True,
        )

    # The paths are already owned. If cleanup cannot even start, preserve that
    # accounting without probing the filesystem after the deadline.
    if time.monotonic() >= deadline:
        temporary_index.artifact_state = "cleanup-uncertain"
        temporary_index.cleanup_state = "cleanup-uncertain"
        raise GitCollectionError(
            _failure_metadata(
                "deadline-exceeded",
                stage,
                temporary_artifact_unresolved_count=_owned_artifact_count(
                    temporary_index
                ) or 2,
                temporary_artifact_cleanup_uncertain=True,
            )
        )

    try:
        errors, unresolved, first_error = _bounded_filesystem_call(
            lambda: _remove_owned_artifacts(temporary_index),
            deadline=deadline,
            stage=stage,
            pending_artifacts=_owned_artifact_count(temporary_index) or 2,
            cleanup=True,
            on_starting=lambda: setattr(temporary_index, "cleanup_state", "may-be-running"),
            on_late_completion=lambda value, error: _late_cleanup_owned_index(
                temporary_index, value, error
            ),
        )
    except BaseException as exc:
        failure = getattr(exc, "git_failure", None)
        if isinstance(failure, GitFailureMetadata):
            temporary_index.artifact_state = "cleanup-uncertain"
            temporary_index.cleanup_state = "cleanup-uncertain"
            if (
                failure.reason == "deadline-exceeded"
                and failure.filesystem_operation_pending_count == 0
            ):
                _attach_failure(
                    exc,
                    _failure_metadata(
                        "deadline-exceeded",
                        stage,
                        temporary_artifact_unresolved_count=(
                            _owned_artifact_count(temporary_index) or 2
                        ),
                        temporary_artifact_cleanup_uncertain=True,
                    ),
                )
            raise
        temporary_index.artifact_state = "cleanup-uncertain"
        temporary_index.cleanup_state = "cleanup-uncertain"
        failure = _failure_metadata(
            "cleanup-failed",
            stage,
            cleanup_error_count=1,
            temporary_artifact_unresolved_count=_owned_artifact_count(temporary_index) or 2,
            temporary_artifact_cleanup_uncertain=True,
        )
        _attach_failure(exc, failure)
        raise

    if errors or unresolved:
        temporary_index.artifact_state = "cleanup-uncertain"
        temporary_index.cleanup_state = "cleanup-uncertain"
        failure = _failure_metadata(
            "cleanup-failed",
            stage,
            cleanup_error_count=errors,
            temporary_artifact_unresolved_count=unresolved,
            temporary_artifact_cleanup_uncertain=bool(unresolved),
        )
        if first_error is not None:
            _attach_failure(first_error, failure)
            raise first_error
        return failure
    temporary_index.artifact_state = "cleanup-confirmed"
    temporary_index.cleanup_state = "cleanup-confirmed"
    return None


def _late_cleanup_owned_index(
    temporary_index: _TemporaryIndex,
    value,
    error: BaseException | None,
) -> None:
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and type(value[0]) is int
        and type(value[1]) is str
        and temporary_index.creation_result is None
    ):
        fd, path_text = value
        temporary_index.creation_result = (fd, path_text)
        temporary_index.descriptor = fd
        temporary_index.descriptor_uncertain = False
        temporary_index.path = Path(path_text)
        temporary_index.lock_path = Path(path_text + ".lock")
        temporary_index.artifact_state = "owned"
    if temporary_index.creation_result is not None and (
        temporary_index.path is None or temporary_index.lock_path is None
    ):
        _fd, path_text = temporary_index.creation_result
        temporary_index.path = Path(path_text)
        temporary_index.lock_path = Path(path_text + ".lock")
        temporary_index.artifact_state = "owned"
    temporary_index.operation_state = "completed"
    if error is not None and temporary_index.creation_result is None:
        temporary_index.artifact_state = "cleanup-uncertain"
        temporary_index.cleanup_state = "cleanup-uncertain"
        return
    errors, unresolved, _first_error = _remove_owned_artifacts(temporary_index)
    if errors or unresolved:
        temporary_index.artifact_state = "cleanup-uncertain"
        temporary_index.cleanup_state = "cleanup-uncertain"
    else:
        temporary_index.artifact_state = "cleanup-confirmed"
        temporary_index.cleanup_state = "cleanup-confirmed"


@contextmanager
def _temporary_index(
    *,
    prefix: str,
    deadline: float,
    init_stage: str,
    cleanup_stage: str,
) -> Iterator[_TemporaryIndex]:
    owner = _TemporaryIndex()

    def cleanup_late_creation(value, error) -> None:
        _late_cleanup_owned_index(owner, value, error)

    def create_temporary_index() -> tuple[int, str]:
        fd, path_text = tempfile.mkstemp(prefix=prefix)
        # Record ownership in the helper immediately after creation, before
        # publishing completion to the caller.
        owner.creation_result = (fd, path_text)
        owner.descriptor = fd
        owner.descriptor_uncertain = False
        owner.path = Path(path_text)
        owner.lock_path = Path(path_text + ".lock")
        owner.artifact_state = "owned"
        owner.operation_state = "completed"
        return fd, path_text

    def reserve_creation_ownership() -> None:
        owner.artifact_state = "may-exist"
        owner.operation_state = "may-be-running"

    try:
        fd, path_text = _bounded_filesystem_call(
            create_temporary_index,
            deadline=deadline,
            stage=init_stage,
            pending_artifacts=2,
            on_starting=reserve_creation_ownership,
            on_late_completion=cleanup_late_creation,
        )
    except BaseException as original:
        if getattr(original, "git_filesystem_operation_pending", False):
            raise
        if owner.artifact_state == "owned":
            try:
                cleanup_failure = _cleanup_temporary_index(
                    owner,
                    deadline=deadline,
                    stage=cleanup_stage,
                )
            except BaseException as cleanup_error:
                cleanup_failure = getattr(cleanup_error, "git_failure", None)
                if not isinstance(cleanup_failure, GitFailureMetadata):
                    cleanup_failure = _failure_metadata(
                        "cleanup-failed",
                        cleanup_stage,
                        cleanup_error_count=1,
                        temporary_artifact_unresolved_count=2,
                        temporary_artifact_cleanup_uncertain=True,
                    )
            if cleanup_failure is not None:
                _attach_failure(original, cleanup_failure, attribute="git_cleanup_failure")
        raise

    try:
        _bounded_filesystem_call(
            lambda: _close_temporary_index_descriptor(owner),
            deadline=deadline,
            stage=init_stage,
            pending_artifacts=2,
            on_late_completion=lambda value, error: _late_cleanup_owned_index(
                owner, value, error
            ),
        )
    except BaseException as original:
        if not getattr(original, "git_filesystem_operation_pending", False):
            try:
                cleanup_failure = _cleanup_temporary_index(
                    owner,
                    deadline=deadline,
                    stage=cleanup_stage,
                )
            except BaseException as cleanup_error:
                cleanup_failure = getattr(cleanup_error, "git_failure", None)
                if not isinstance(cleanup_failure, GitFailureMetadata):
                    cleanup_failure = _failure_metadata(
                        "cleanup-failed",
                        cleanup_stage,
                        cleanup_error_count=1,
                        temporary_artifact_unresolved_count=2,
                        temporary_artifact_cleanup_uncertain=True,
                    )
            if cleanup_failure is not None:
                _attach_failure(original, cleanup_failure, attribute="git_cleanup_failure")
        raise

    try:
        yield owner
    except BaseException as original:
        if getattr(original, "git_filesystem_operation_pending", False):
            raise
        try:
            cleanup_failure = _cleanup_temporary_index(
                owner,
                deadline=deadline,
                stage=cleanup_stage,
            )
        except BaseException as cleanup_error:
            cleanup_failure = getattr(cleanup_error, "git_failure", None)
            if not isinstance(cleanup_failure, GitFailureMetadata):
                cleanup_failure = _failure_metadata(
                    "cleanup-failed",
                    cleanup_stage,
                    cleanup_error_count=1,
                    temporary_artifact_unresolved_count=2,
                    temporary_artifact_cleanup_uncertain=True,
                )
        if cleanup_failure is not None:
            _attach_failure(original, cleanup_failure, attribute="git_cleanup_failure")
        raise
    else:
        cleanup_failure = _cleanup_temporary_index(
            owner,
            deadline=deadline,
            stage=cleanup_stage,
        )
        if cleanup_failure is not None:
            raise GitCollectionError(cleanup_failure)


def _copy_index_sync(
    index_path: Path,
    temporary_index: _TemporaryIndex,
    *,
    deadline: float,
) -> None:
    _remaining(deadline, "cumulative-patch-index-copy")
    try:
        index_path.stat()
    except FileNotFoundError:
        with temporary_index.path.open("wb"):
            pass
        _remaining(deadline, "cumulative-patch-index-copy")
        return

    with index_path.open("rb") as source, temporary_index.path.open("wb") as target:
        while True:
            _remaining(deadline, "cumulative-patch-index-copy")
            chunk = source.read(_INDEX_COPY_CHUNK_BYTES)
            if not chunk:
                break
            written = target.write(chunk)
            if written != len(chunk):
                raise OSError("incomplete temporary-index copy")
    _remaining(deadline, "cumulative-patch-index-copy")


def _copy_index(index_path: Path, temporary_index: _TemporaryIndex, *, deadline: float) -> None:
    _bounded_filesystem_call(
        lambda: _copy_index_sync(index_path, temporary_index, deadline=deadline),
        deadline=deadline,
        stage="cumulative-patch-index-copy",
        pending_artifacts=2,
        on_late_completion=lambda value, error: _late_cleanup_owned_index(
            temporary_index, value, error
        ),
    )

def _capture_patch_with_untracked(
    *,
    repo: Path,
    baseline_commit: str,
    untracked_files: list[str],
    deadline: float,
) -> str:
    index_path_text = _git(
        repo,
        "rev-parse",
        "--git-path",
        "index",
        deadline=deadline,
        stage="cumulative-patch-index-path",
    ).strip()
    if not index_path_text:
        _raise_git_failure("incomplete-collection", "cumulative-patch-index-path")

    index_path = Path(index_path_text)
    if not index_path.is_absolute():
        index_path = Path(os.path.abspath(os.fspath(repo / index_path)))

    init_stage = "cumulative-patch-index-init"
    _remaining(deadline, "cumulative-patch-index-copy")
    with _temporary_index(
        prefix="coding-agent-harness-index-",
        deadline=deadline,
        init_stage=init_stage,
        cleanup_stage="cumulative-patch-index-cleanup",
    ) as temporary_index:
        _copy_index(index_path, temporary_index, deadline=deadline)
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(temporary_index.path)

        if untracked_files:
            _git(
                repo,
                "add",
                "--",
                *untracked_files,
                env=env,
                deadline=deadline,
                stage="cumulative-patch-index-add",
                temporary_index=temporary_index,
            )

        return _git(
            repo,
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            baseline_commit,
            "--",
            env=env,
            deadline=deadline,
            stage="cumulative-patch",
            stdout_limit_bytes=GIT_PATCH_STDOUT_LIMIT_BYTES,
            temporary_index=temporary_index,
        )

def capture_evidence(*, repo: Path, baseline: GitBaseline) -> GitEvidence:
    partial = {"baseline_commit": baseline.commit}
    stage = "cumulative-head"
    try:
        deadline = _new_deadline(CUMULATIVE_TIMEOUT_SECONDS)

        final_head = _require_object_id(
            _git(repo, "rev-parse", "HEAD", deadline=deadline, stage=stage),
            stage,
        )
        partial["final_head"] = final_head

        stage = "cumulative-status"
        final_status = _status(repo, deadline=deadline, stage=stage)
        partial["final_status"] = final_status

        stage = "cumulative-tracked-files"
        tracked_changes = _git_z(
            repo,
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            baseline.commit,
            "--",
            deadline=deadline,
            stage=stage,
        )
        partial["tracked_changes"] = tracked_changes

        stage = "cumulative-untracked-files"
        untracked_files = _git_z(
            repo,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            deadline=deadline,
            stage=stage,
        )
        changed_files = sorted(set(tracked_changes + untracked_files))
        partial["changed_files"] = changed_files
        _remaining(deadline, stage)

        stage = "cumulative-patch"
        patch = _capture_patch_with_untracked(
            repo=repo,
            baseline_commit=baseline.commit,
            untracked_files=untracked_files,
            deadline=deadline,
        )
        partial["patch"] = patch

        stage = "cumulative-commits"
        commits_output = _git(
            repo,
            "log",
            "--format=%H%x09%s",
            f"{baseline.commit}..{final_head}",
            deadline=deadline,
            stage=stage,
        )
        commits = [line for line in commits_output.splitlines() if line.strip()]
        _remaining(deadline, stage)
        partial["commits"] = commits

        return GitEvidence(
            baseline_commit=baseline.commit,
            final_head=final_head,
            final_status=final_status,
            changed_files=changed_files,
            patch=patch,
            commits=commits,
        )
    except BaseException as exc:
        _attach_unexpected_failure(exc, partial, stage)
        raise


def _capture_worktree_tree(
    repo: Path,
    *,
    deadline: float,
    observations: dict,
    observation_key: str,
    stage_prefix: str,
) -> str:
    env = os.environ.copy()
    init_stage = f"{stage_prefix}-index-init"
    with _temporary_index(
        prefix="coding-agent-harness-snapshot-index-",
        deadline=deadline,
        init_stage=init_stage,
        cleanup_stage=f"{stage_prefix}-cleanup",
    ) as temporary_index:
        _bounded_filesystem_call(
            lambda: temporary_index.path.unlink(missing_ok=True),
            deadline=deadline,
            stage=init_stage,
            pending_artifacts=2,
            on_late_completion=lambda value, error: _late_cleanup_owned_index(
                temporary_index, value, error
            ),
        )
        env["GIT_INDEX_FILE"] = str(temporary_index.path)

        _git(
            repo,
            "read-tree",
            "HEAD",
            env=env,
            deadline=deadline,
            stage=f"{stage_prefix}-read-tree",
            temporary_index=temporary_index,
        )

        _git(
            repo,
            "add",
            "-A",
            "--",
            ".",
            env=env,
            deadline=deadline,
            stage=f"{stage_prefix}-add",
            temporary_index=temporary_index,
        )

        tree = _require_object_id(
            _git(
                repo,
                "write-tree",
                env=env,
                deadline=deadline,
                stage=f"{stage_prefix}-write-tree",
                temporary_index=temporary_index,
            ),
            f"{stage_prefix}-write-tree",
        )
        observations[observation_key] = tree

    _remaining(deadline, f"{stage_prefix}-cleanup")
    return tree

def capture_worktree_snapshot(repo: Path) -> GitWorktreeSnapshot:
    partial: dict = {}
    stage = "snapshot-index-init"
    try:
        deadline = _new_deadline(SNAPSHOT_TIMEOUT_SECONDS)
        tree = _capture_worktree_tree(
            repo,
            deadline=deadline,
            observations=partial,
            observation_key="tree",
            stage_prefix="snapshot",
        )
        return GitWorktreeSnapshot(tree=tree)
    except BaseException as exc:
        _attach_unexpected_failure(exc, partial, stage)
        raise


def capture_attempt_evidence(
    *,
    repo: Path,
    before: GitWorktreeSnapshot,
) -> GitAttemptEvidence:
    partial = {"before_tree": before.tree}
    stage = "attempt-after-snapshot-index-init"
    try:
        deadline = _new_deadline(ATTEMPT_EVIDENCE_TIMEOUT_SECONDS)
        after = _capture_worktree_tree(
            repo,
            deadline=deadline,
            observations=partial,
            observation_key="after_tree",
            stage_prefix="attempt-after-snapshot",
        )
        partial["after_tree"] = after

        stage = "attempt-diff"
        changed_files = sorted(
            set(
                _git_z(
                    repo,
                    "diff",
                    "--name-only",
                    "--no-renames",
                    "-z",
                    before.tree,
                    after,
                    "--",
                    deadline=deadline,
                    stage=stage,
                )
            )
        )
        _remaining(deadline, stage)
        partial["changed_files"] = changed_files

        return GitAttemptEvidence(
            before_tree=before.tree,
            after_tree=after,
            changed_files=changed_files,
        )
    except BaseException as exc:
        _attach_unexpected_failure(exc, partial, stage)
        raise
