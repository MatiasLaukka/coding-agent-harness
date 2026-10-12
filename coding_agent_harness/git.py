from __future__ import annotations

import math
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from coding_agent_harness.models import StrictModel
from coding_agent_harness.process import ProcessResult, run_process


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
) -> GitFailureMetadata:
    return GitFailureMetadata(
        reason=reason,
        stage=stage if stage in _GIT_FAILURE_STAGES else "incomplete-collection",
        exit_code=process_result.exit_code if process_result is not None else None,
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=stderr_limit_bytes,
        stdout_truncated=process_result.stdout_truncated if process_result is not None else False,
        stderr_truncated=process_result.stderr_truncated if process_result is not None else False,
        cleanup_error_count=len(process_result.cleanup_errors) if process_result is not None else 0,
        surviving_process_count=len(process_result.surviving_pids) if process_result is not None else 0,
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


def _git(
    repo: Path,
    *args: str,
    deadline: float,
    stage: str,
    env: dict[str, str] | None = None,
    stdout_limit_bytes: int = GIT_STDOUT_LIMIT_BYTES,
) -> str:
    remaining = _remaining(deadline, stage)
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


@contextmanager
def _temporary_index(*, prefix: str, cleanup_stage: str) -> Iterator[Path]:
    fd, path_text = tempfile.mkstemp(prefix=prefix)
    path = Path(path_text)
    try:
        os.close(fd)
        yield path
    except BaseException as original:
        try:
            path.unlink(missing_ok=True)
        except BaseException as cleanup_error:
            _attach_failure(
                original,
                _failure_metadata("cleanup-failed", cleanup_stage),
                attribute="git_cleanup_failure",
            )
        raise
    else:
        try:
            path.unlink(missing_ok=True)
        except BaseException as cleanup_error:
            _attach_failure(
                cleanup_error,
                _failure_metadata("cleanup-failed", cleanup_stage),
            )
            raise


def _copy_index(index_path: Path, temporary_index: Path, *, deadline: float) -> None:
    _remaining(deadline, "cumulative-patch-index-copy")
    if not index_path.exists():
        temporary_index.write_bytes(b"")
        _remaining(deadline, "cumulative-patch-index-copy")
        return

    with index_path.open("rb") as source, temporary_index.open("wb") as target:
        while True:
            _remaining(deadline, "cumulative-patch-index-copy")
            chunk = source.read(_INDEX_COPY_CHUNK_BYTES)
            if not chunk:
                break
            target.write(chunk)
    _remaining(deadline, "cumulative-patch-index-copy")


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
        index_path = (repo / index_path).resolve()

    _remaining(deadline, "cumulative-patch-index-copy")
    with _temporary_index(
        prefix="coding-agent-harness-index-",
        cleanup_stage="cumulative-patch-index-cleanup",
    ) as temporary_index:
        _copy_index(index_path, temporary_index, deadline=deadline)
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(temporary_index)

        if untracked_files:
            _git(
                repo,
                "add",
                "--",
                *untracked_files,
                env=env,
                deadline=deadline,
                stage="cumulative-patch-index-add",
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
    with _temporary_index(
        prefix="coding-agent-harness-snapshot-index-",
        cleanup_stage=f"{stage_prefix}-cleanup",
    ) as temporary_index:
        stage = f"{stage_prefix}-index-init"
        _remaining(deadline, stage)
        temporary_index.unlink(missing_ok=True)
        env["GIT_INDEX_FILE"] = str(temporary_index)

        _git(
            repo,
            "read-tree",
            "HEAD",
            env=env,
            deadline=deadline,
            stage=f"{stage_prefix}-read-tree",
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
        )

        tree = _require_object_id(
            _git(
                repo,
                "write-tree",
                env=env,
                deadline=deadline,
                stage=f"{stage_prefix}-write-tree",
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
